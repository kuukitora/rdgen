"""Inject the optional Windows service inventory module into RustDesk 1.5.3."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit


def console_origin(value):
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid console port') from exc
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Console URL must be an HTTPS origin without a path or credentials')
    host = parsed.hostname
    if not all(c.isascii() and (c.isalnum() or c in '.-') for c in host):
        raise ValueError('Console host must be a DNS name or IPv4 address')
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid console port')
    return 'wss://' + host.lower() + ((':' + str(port)) if port else '')


def patch(root, template, origin):
    root = Path(root)
    lib = root / 'src/lib.rs'
    windows = root / 'src/platform/windows.rs'
    websocket = root / 'libs/hbb_common/src/websocket.rs'
    lib_text = lib.read_text(encoding='utf-8')
    win_text = windows.read_text(encoding='utf-8')
    ws_text = websocket.read_text(encoding='utf-8')
    needle = '    status_handle.set_service_status(next_status)?;'
    if lib_text.count('mod rdgen_inventory;') or win_text.count(needle) != 1:
        raise ValueError('Unsupported RustDesk service layout')
    ws_needle = '    pub async fn new<T: AsRef<str>>(\n'
    if ws_text.count(ws_needle) != 1 or 'pub async fn new_rdgen_strict' in ws_text:
        raise ValueError('Unsupported RustDesk WebSocket layout')
    strict = '''    pub async fn new_rdgen_strict(url: &str, ms_timeout: u64) -> ResultType<Self> {
        if !url.starts_with("wss://") {
            bail!("Inventory requires secure WebSocket");
        }
        let proxy_conf = Config::get_socks();
        let (stream, addr) = Self::try_connect(
            url, None, proxy_conf.as_ref(), ms_timeout,
            TlsType::Rustls, false, Some(false), Some(false),
        ).await?;
        Ok(Self { stream, addr, encrypt: None, send_timeout: ms_timeout })
    }

'''
    source = Path(template).read_text(encoding='utf-8')
    marker = '__RDGEN_CONSOLE_ORIGIN__'
    if source.count(marker) != 1:
        raise ValueError('Inventory source template is invalid')
    source = source.replace(marker, json.dumps(console_origin(origin)))
    lib.write_text('#[cfg(target_os = "windows")]\nmod rdgen_inventory;\n' + lib_text, encoding='utf-8')
    windows.write_text(win_text.replace(needle, needle + '\n    tokio::spawn(crate::rdgen_inventory::run());', 1), encoding='utf-8')
    websocket.write_text(ws_text.replace(ws_needle, strict + ws_needle, 1), encoding='utf-8')
    (root / 'src/rdgen_inventory.rs').write_text(source, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--origin', required=True)
    args = parser.parse_args()
    patch(args.root, args.template, args.origin)
