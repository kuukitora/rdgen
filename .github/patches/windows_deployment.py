"""Configure a background Windows service installer and a signed update feed."""

import argparse
import base64
import json
import re
from pathlib import Path


def replace_once(text, old, new, description):
    if text.count(old) != 1:
        raise ValueError(f"RustDesk's {description} has changed.")
    return text.replace(old, new, 1)


def prepare_msi(root):
    package = root / "res/msi/Package"
    # AnotherApp is a regular dialog, so error-dialog result ErrorAbort is
    # invalid and produces MSI error 2866 when the user clicks Cancel.
    conflict_dialog = package / 'UI/AnotherApp.wxs'
    source = conflict_dialog.read_text(encoding='utf-8')
    source = replace_once(source, '<Publish Event="EndDialog" Value="ErrorAbort" />',
                          '<Publish Event="EndDialog" Value="Exit" />',
                          'MSI existing-installation conflict dialog')
    conflict_dialog.write_text(source, encoding='utf-8')
    components = package / "Components/RustDesk.wxs"
    source = components.read_text(encoding="utf-8")
    source, count = re.subn(
        r'(?m)^[ \t]*<Custom Action="(?:LaunchApp|LaunchAppTray)"[^\n]*\n', "", source,
    )
    if count != 2:
        raise ValueError("RustDesk's MSI application launch actions have changed.")
    # An old user profile can retain stop-service=Y after uninstall. For these
    # service installers that must not silently suppress service creation.
    service_condition = '(NOT STOP_SERVICE=&quot;&apos;Y&apos;&quot;) AND '
    if source.count(service_condition) != 2:
        raise ValueError("RustDesk's MSI service creation conditions have changed.")
    source = source.replace(service_condition, '')
    # Removing the previous product during a major upgrade also sets REMOVE=ALL.
    # Its strict service-start action must not restart the product being removed.
    uninstall_condition = '(NOT (Installed AND REMOVE AND NOT UPGRADINGPRODUCTCODE))'
    for action in ('CreateStartService', 'CreateStartService.SetParam'):
        match = re.search(rf'<Custom Action="{re.escape(action)}"[^\n]+', source)
        if not match or uninstall_condition not in match[0]:
            raise ValueError("RustDesk's MSI service removal condition has changed.")
        source = replace_once(source, match[0], match[0].replace(
            uninstall_condition, '(REMOVE&lt;&gt;&quot;ALL&quot;)'), f'MSI {action} sequence')
    components.write_text(source, encoding="utf-8")

    shortcuts = package / "Fragments/ShortcutProperties.wxs"
    source = shortcuts.read_text(encoding="utf-8")
    source = replace_once(source, 'Id="STARTUPSHORTCUTS" Value="1"',
                          'Id="STARTUPSHORTCUTS" Value="0"', "MSI logon shortcut setting")
    shortcuts.write_text(source, encoding="utf-8")

    actions = package / 'Fragments/CustomActions.wxs'
    source = actions.read_text(encoding='utf-8')
    source = replace_once(source,
        'DllEntry="CreateStartService" Impersonate="no" Execute="deferred" Return="ignore"',
        'DllEntry="CreateStartService" Impersonate="no" Execute="deferred" Return="check"',
        'MSI service action error handling')
    actions.write_text(source, encoding='utf-8')

    service_utils = root / 'res/msi/CustomActions/ServiceUtils.cpp'
    source = service_utils.read_text(encoding='utf-8')
    source = replace_once(source,
        '    SERVICE_STATUS_PROCESS serviceStatus;\n    QueryServiceStatusExW(serviceName, &serviceStatus);\n    return (serviceStatus.dwCurrentState == SERVICE_RUNNING);',
        '    SERVICE_STATUS_PROCESS serviceStatus = {};\n    if (!QueryServiceStatusExW(serviceName, &serviceStatus)) return false;\n    return (serviceStatus.dwCurrentState == SERVICE_RUNNING);',
        'MSI service status query')
    service_utils.write_text(source, encoding='utf-8')

    cpp = root / 'res/msi/CustomActions/CustomActions.cpp'
    source = cpp.read_text(encoding='utf-8')
    marker = 'void TryCreateStartServiceByShell(LPWSTR svcName, LPWSTR svcBinary, LPWSTR szSvcDisplayName);'
    helper = '''// Confirm an existing service is the service this MSI is installing.
// A running service with the same name but an old executable must not count.
bool RdgenServiceMatches(LPCWSTR name, LPCWSTR binary) {
    SC_HANDLE manager = OpenSCManagerW(NULL, NULL, SC_MANAGER_CONNECT);
    if (!manager) return false;
    SC_HANDLE service = OpenServiceW(manager, name, SERVICE_QUERY_CONFIG);
    if (!service) { CloseServiceHandle(manager); return false; }
    DWORD bytes = 0;
    QueryServiceConfigW(service, NULL, 0, &bytes);
    LPQUERY_SERVICE_CONFIGW config = bytes ? (LPQUERY_SERVICE_CONFIGW)LocalAlloc(LPTR, bytes) : NULL;
    bool matches = config && QueryServiceConfigW(service, config, bytes, &bytes)
        && config->dwStartType == SERVICE_AUTO_START
        && config->lpServiceStartName && _wcsicmp(config->lpServiceStartName, L"LocalSystem") == 0
        && config->lpBinaryPathName && _wcsicmp(config->lpBinaryPathName, binary) == 0;
    if (config) LocalFree(config);
    CloseServiceHandle(service);
    CloseServiceHandle(manager);
    return matches;
}

'''
    source = replace_once(source, marker, helper + marker, 'MSI service configuration check')
    source = replace_once(source,
        '        WcaLog(LOGMSG_STANDARD, "Failed to find binary : %ls", svcParams);\n        goto LExit;',
        '        WcaLog(LOGMSG_STANDARD, "Failed to find binary : %ls", svcParams);\n        hr = E_INVALIDARG;\n        goto LExit;',
        'MSI invalid service parameters')
    source = replace_once(source, r'L"create %ls binpath=', r'L"create \"%ls\" binpath=', 'MSI service name quoting')
    source = replace_once(source, r'L"/c sc start %ls"', r'L"/c sc start \"%ls\""', 'MSI service start quoting')
    source = replace_once(source, r'L"/c sc stop %ls"', r'L"/c sc stop \"%ls\""', 'MSI service stop quoting')
    source = replace_once(source, r'L"/c sc delete %ls"', r'L"/c sc delete \"%ls\""', 'MSI service removal quoting')
    source = replace_once(source,
        '    if (IsServiceRunningW(svcName)) {\n        WcaLog(LOGMSG_STANDARD, "Service \\"%ls\\" is running.", svcName);',
        '''    // StartServiceW may return while the service is still START_PENDING.
    // Allow it time to finish before the shell fallback deletes/recreates it.
    if (RdgenServiceMatches(svcName, svcBinary) && !IsServiceRunningW(svcName)) {
        MyStartServiceW(svcName);
        for (int attempt = 0; attempt < 150 && !IsServiceRunningW(svcName); ++attempt) {
            Sleep(200);
        }
    }
    if (IsServiceRunningW(svcName) && RdgenServiceMatches(svcName, svcBinary)) {
        WcaLog(LOGMSG_STANDARD, "Service \\"%ls\\" is running.", svcName);''',
        'MSI existing service configuration validation')
    fallback = '        TryCreateStartServiceByShell(svcName, svcBinary, szSvcDisplayName);\n    }\n\nLExit:'
    checked = '''        TryCreateStartServiceByShell(svcName, svcBinary, szSvcDisplayName);
    }
    // Shell fallback is asynchronous; wait for SCM to report RUNNING rather
    // than returning a successful installation with no usable service.
    for (int attempt = 0; attempt < 150 && !(IsServiceRunningW(svcName) && RdgenServiceMatches(svcName, svcBinary)); ++attempt) {
        Sleep(200);
    }
    if (!(IsServiceRunningW(svcName) && RdgenServiceMatches(svcName, svcBinary))) {
        WcaLog(LOGMSG_STANDARD, "Service did not reach RUNNING with the expected executable and automatic start.");
        MSIHANDLE message = MsiCreateRecord(1);
        if (message) {
            MsiRecordSetStringW(message, 0, L"The background service could not start with the expected configuration. Installation cannot complete. Check Windows services and security software restrictions, then retry with administrator privileges. If it still fails, run msiexec /i <installer.msi> /L*v <logfile> and send the log to support.");
            MsiProcessMessage(hInstall, INSTALLMESSAGE_ERROR, message);
            MsiCloseHandle(message);
        }
        hr = HRESULT_FROM_WIN32(ERROR_SERVICE_NOT_ACTIVE);
    }

LExit:'''
    english_help = ('The background service could not start with the expected configuration. '
                    'Installation cannot complete. Check Windows services and security software restrictions, '
                    'then retry with administrator privileges. If it still fails, run msiexec /i <installer.msi> '
                    '/L*v <logfile> and send the log to support.')
    chinese_help = ('后台服务未能按预期启动，安装无法完成。请检查 Windows 服务及安全软件限制，'
                    '然后以管理员权限重试。如仍失败，请运行 msiexec /i <安装包路径> /L*v <日志路径> '
                    '并将日志交给技术支持。')
    checked = checked.replace(english_help, chinese_help.encode('unicode_escape').decode('ascii'))
    source = replace_once(source, fallback, checked, 'MSI service startup verification')
    cpp.write_text(source, encoding='utf-8')


def patch_updates(root, feed, public_key):
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/updates/(host|admin)/[0-9a-f]{64}", feed):
        raise ValueError("The Windows update feed must be a configured RDGen HTTPS channel.")
    key = base64.b64decode(public_key, validate=True)
    if len(key) != 32:
        raise ValueError("The update public key must have 32 bytes.")

    hbb = root / "libs/hbb_common/src/lib.rs"
    source = hbb.read_text(encoding="utf-8")
    source = replace_once(source, 'const URL: &str = "https://api.rustdesk.com/version/latest";',
                          f'const URL: &str = {json.dumps(feed + "/api/version/latest")};',
                          "update check endpoint")
    hbb.write_text(source, encoding="utf-8")

    patch_release_validation(root, feed)
    patch_artifact_location(root, feed)
    patch_trusted_key(root, key)
    patch_verified_msi_name(root)

    for relative in ("src/updater.rs", "src/flutter_ffi.rs"):
        path = root / relative
        source = path.read_text(encoding="utf-8")
        source = replace_once(source, "is_msi_installed()? && !crate::is_custom_client()",
                              "is_msi_installed()?", "installed MSI update format")
        path.write_text(source, encoding="utf-8")

    updater = root / "src/updater.rs"
    source = updater.read_text(encoding="utf-8")
    source = replace_once(source, "    do_check_software_update()?;",
                          "    if !update_msi { return Ok(()); }\n    do_check_software_update()?;",
                          "installed MSI update guard")
    updater.write_text(source, encoding="utf-8")


def patch_verified_msi_name(root):
    """Allow spaces only in the verified MSI handoff, whose command omits the name."""
    handoff = root / "src/platform/windows/installer_handoff.rs"
    source = handoff.read_text(encoding="utf-8")
    old = '''pub(super) fn run_cmds(cmds: impl Into<InstallCommands>, show: bool, tip: &str) -> ResultType<()> {
    validate_install_app_name(&crate::get_app_name())?;
    let path = directory::path()?.with_extension("bat");'''
    new = '''fn validate_verified_msi_app_name(name: &str) -> ResultType<()> {
    let mut bytes = name.bytes();
    let Some(first) = bytes.next() else { bail!("Application name must not be empty."); };
    if !(first.is_ascii_alphanumeric() || first == b'_' || first == b'-')
        || name.ends_with(' ')
        || !bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b' ')
    {
        bail!("Unsafe application name for verified MSI update.");
    }
    Ok(())
}

pub(super) fn run_verified_msi_cmds(cmds: impl Into<InstallCommands>, show: bool, tip: &str) -> ResultType<()> {
    validate_verified_msi_app_name(&crate::get_app_name())?;
    run_cmds_inner(cmds, show, tip)
}

pub(super) fn run_cmds(cmds: impl Into<InstallCommands>, show: bool, tip: &str) -> ResultType<()> {
    validate_install_app_name(&crate::get_app_name())?;
    run_cmds_inner(cmds, show, tip)
}

fn run_cmds_inner(cmds: impl Into<InstallCommands>, show: bool, tip: &str) -> ResultType<()> {
    let path = directory::path()?.with_extension("bat");'''
    source = replace_once(source, old, new, "verified MSI handoff name validation")
    marker = '    #[test]\n    fn protected_handoff_preserves_shortcuts_failures_and_cleanup()'
    tests = '''    #[test]
    fn verified_msi_names_allow_spaces_but_reject_shell_metacharacters() {
        for name in ["Arcade Connect", "Arcade Connect Admin", "RustDesk", "-Client"] {
            assert!(validate_verified_msi_app_name(name).is_ok(), "{name}");
        }
        for name in ["", " leading", "trailing ", "name&cmd", "name|cmd", "name%PATH%", "name\\\"x", "name.x"] {
            assert!(validate_verified_msi_app_name(name).is_err(), "{name}");
        }
    }

'''
    source = replace_once(source, marker, tests + marker, "verified MSI name tests")
    handoff.write_text(source, encoding="utf-8")

    verified = root / "src/platform/windows/verified_update.rs"
    source = verified.read_text(encoding="utf-8")
    source = replace_once(source, 'installer_handoff::{run_cmds, InstallCommands},',
                          'installer_handoff::{run_cmds, run_verified_msi_cmds, InstallCommands},',
                          'verified MSI handoff import')
    source = replace_once(source,
        '''    run_cmds(
        InstallCommands {
            script: verified_msi_install_commands(update.path(), quiet)?,''',
        '''    run_verified_msi_cmds(
        InstallCommands {
            script: verified_msi_install_commands(update.path(), quiet)?,''',
        'verified MSI handoff call')
    verified.write_text(source, encoding="utf-8")


def patch_release_validation(root, feed):
    common = root / "src/common.rs"
    source = common.read_text(encoding="utf-8")
    start = source.index("    let url = url::Url::parse(update_url)?;")
    end = source.index("    if release_id.is_empty()", start)
    prefix = json.dumps(feed + "/rustdesk/rustdesk/releases/tag/")
    source = source[:start] + (
        f'    let release_id = update_url.strip_prefix({prefix})\n'
        '        .ok_or_else(|| hbb_common::anyhow::anyhow!("Unexpected Windows update channel"))?;\n'
    ) + source[end:]
    start = source.index("pub(crate) fn display_version_from_release_id(")
    end = source.index("\nfn clear_software_update_url()", start)
    source = source[:start] + '''fn rdgen_update_version(version: &str) -> Option<(u16, u16, u16, u16)> {
    let (base, build) = match version.split_once('+') {
        Some((base, build)) if build.len() == 3 && build.bytes().all(|b| b.is_ascii_digit()) => {
            (base, build.parse().ok()?)
        }
        Some(_) => return None,
        None => (version, 0),
    };
    let mut parts = base.split('.');
    let (major, minor, patch) = (
        parts.next()?.parse::<u16>().ok()?,
        parts.next()?.parse::<u16>().ok()?,
        parts.next()?.parse::<u16>().ok()?,
    );
    if parts.next().is_some() || major > 255 || minor > 255 || (major, minor, patch) == (0, 0, 0) {
        return None;
    }
    Some((major, minor, patch, build))
}

pub(crate) fn display_version_from_release_id(release_id: &str) -> ResultType<String> {
    let version = release_id.strip_prefix('v').unwrap_or(release_id);
    if rdgen_update_version(version).is_none() {
        bail!("Invalid Windows release version");
    }
    Ok(version.to_string())
}
''' + source[end:]
    source = replace_once(source,
                          'if get_version_number(&latest_release_version) > get_version_number(crate::VERSION) {',
                          'if rdgen_update_version(&latest_release_version) > rdgen_update_version(crate::VERSION) {',
                          "online update version comparison")
    common.write_text(source, encoding="utf-8")


def patch_artifact_location(root, feed):
    artifact = root / "src/updater/artifact.rs"
    source = artifact.read_text(encoding="utf-8")
    old = 'format!("https://github.com/rustdesk/rustdesk/releases/download/{release_id}/")'
    download = feed + "/rustdesk/rustdesk/releases/download/"
    source = replace_once(source, old, f'format!({json.dumps(download + "{release_id}/")})',
                          "signed metadata endpoint")
    start = source.index("    let parsed = url::Url::parse(url).ok()?;")
    end = source.index("    Some(std::env::temp_dir().join(filename))", start)
    source = source[:start] + f'''    let relative_path = url.strip_prefix({json.dumps(download)})?;
    let (tag, filename) = relative_path.split_once('/')?;
    if display_version_from_release_id(tag).is_err()
        || !filename.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        || !is_plain_update_filename(filename)
    {{
        return None;
    }}

''' + source[end:]
    artifact.write_text(source, encoding="utf-8")


def patch_trusted_key(root, key):
    metadata = root / "src/update_metadata.rs"
    source = metadata.read_text(encoding="utf-8")
    public_key_array = ", ".join(str(byte) for byte in key)
    source, count = re.subn(
        r'(?s)const TRUSTED_UPDATE_KEYS: &\[TrustedUpdateKey\] = &\[.*?\];',
        f'''const TRUSTED_UPDATE_KEYS: &[TrustedUpdateKey] = &[TrustedUpdateKey {{
    key_id: "custom-update",
    algorithm: SIGNATURE_ALGORITHM,
    public_key: [{public_key_array}],
}}];''',
        source,
    )
    if count != 1:
        raise ValueError("RustDesk's trusted update keys have changed.")
    metadata.write_text(source, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("msi", "update"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--feed", default="")
    parser.add_argument("--public-key", default="")
    args = parser.parse_args()
    if args.phase == "msi":
        prepare_msi(args.root)
    else:
        patch_updates(args.root, args.feed, args.public_key)


if __name__ == "__main__":
    main()
