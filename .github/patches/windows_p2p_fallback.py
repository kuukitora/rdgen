"""Extend RustDesk 1.5.3's WebRTC fallback to all failed direct handshakes."""

from pathlib import Path
import argparse


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError('RustDesk connection fallback layout has changed')
    return source.replace(old, new, 1)


def patch(root):
    path = Path(root) / 'src/client.rs'
    source = path.read_text(encoding='utf-8')
    source = replace_once(source,
        'let (mut conn, kcp, mut typ, mut direct) = match direct_result {',
        'let (mut conn, mut kcp, mut typ, mut direct) = match direct_result {')
    source = replace_once(source,
        'Err(e) if typ == "WebRTC" && !relay_server.is_empty() => {',
        'Err(e) if typ != "Relay" && !relay_server.is_empty() => {')
    source = replace_once(source,
        'log::warn!("WebRTC secure handshake failed ({}), falling back to relay", e);',
        'log::warn!("Direct secure handshake failed ({}), falling back to relay", e);')
    source = replace_once(source,
        "// WebRTC won the race but identity/DTLS binding failed; fall back to a freshly\n"
        "                // coordinated relay instead of failing the whole attempt. The guard is dropped\n"
        "                // first so the bad pc is closed promptly.",
        "// A transport can report TCP success even when a proxy later resets the\n"
        "                // secure handshake. Retry through a freshly coordinated relay.\n"
        "                // Drop any WebRTC offerer before switching transports.")
    source = replace_once(source,
        '                                conn = relay_conn;\n                                typ = "Relay";',
        '                                conn = relay_conn;\n                                kcp = None;\n                                typ = "Relay";')
    # A policy-forced relay can replace an already established KCP transport.
    # Do not keep the abandoned KCP endpoint alive for the relay session.
    force_start = source.index('        if (interface.is_force_relay() && typ != "WebRTC") || conn.is_err() {')
    force_end = source.index('        let mut conn = conn?;', force_start)
    section = replace_once(source[force_start:force_end],
        '                typ = "Relay";\n                direct = false;',
        '                kcp = None;\n                typ = "Relay";\n                direct = false;')
    source = source[:force_start] + section + source[force_end:]
    start = source.index('        let res = Self::secure_connection(peer_id, signed_id_pk.clone(), key, &mut conn).await;')
    end = source.index('        if typ == "WebRTC" {', start)
    section = replace_once(source[start:end],
        '"WebRTC secure failed ({}); relay fallback also failed: {}",',
        '"Direct secure failed ({}); relay fallback also failed: {}",')
    source = source[:start] + section + source[end:]
    path.write_text(source, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    patch(parser.parse_args().root)
