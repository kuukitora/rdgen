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
    components = package / "Components/RustDesk.wxs"
    source = components.read_text(encoding="utf-8")
    source, count = re.subn(
        r'(?m)^[ \t]*<Custom Action="(?:LaunchApp|LaunchAppTray)"[^\n]*\n', "", source,
    )
    if count != 2:
        raise ValueError("RustDesk's MSI application launch actions have changed.")
    components.write_text(source, encoding="utf-8")

    shortcuts = package / "Fragments/ShortcutProperties.wxs"
    source = shortcuts.read_text(encoding="utf-8")
    source = replace_once(source, 'Id="STARTUPSHORTCUTS" Value="1"',
                          'Id="STARTUPSHORTCUTS" Value="0"', "MSI logon shortcut setting")
    shortcuts.write_text(source, encoding="utf-8")


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
