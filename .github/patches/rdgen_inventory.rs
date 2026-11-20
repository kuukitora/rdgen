//! Read-only BetterDesk device inventory, running inside RustDesk's Windows service.
//! This module is enabled only when an HTTPS console endpoint was configured at build time.

use hbb_common::{base64::{engine::general_purpose::STANDARD, Engine as _},
    config::Config, log, sodiumoxide::crypto::sign, tokio, websocket::WsFramedStream};
use serde_json::{json, Value};
use std::{os::windows::fs::MetadataExt, path::{Component, Path}, time::Duration};
use tokio::{process::Command, time::{sleep, timeout}};

const CONSOLE_ORIGIN: &str = __RDGEN_CONSOLE_ORIGIN__;
const CREATE_NO_WINDOW: u32 = 0x08000000;
const CHALLENGE_PREFIX: &str = "betterdesk-embedded-agent-v1\n";

#[link(name = "kernel32")]
extern "system" {
    fn GetLogicalDrives() -> u32;
    fn GetDriveTypeW(root: *const u16) -> u32;
}

// Mapped network drives have drive-letter paths even after canonicalization.
fn local_drive_root(path: &str) -> Option<String> {
    let tail = path.strip_prefix(r"\\?\")?;
    let bytes = tail.as_bytes();
    if bytes.len() < 3 || !bytes[0].is_ascii_alphabetic() || bytes[1] != b':' || bytes[2] != b'\\' {
        return None;
    }
    Some(tail[..3].to_string())
}

fn is_local_drive(root: &str) -> bool {
    let wide: Vec<u16> = root.encode_utf16().chain(std::iter::once(0)).collect();
    // DRIVE_REMOTE = 4. UNKNOWN and NO_ROOT_DIR are rejected too.
    matches!(unsafe { GetDriveTypeW(wide.as_ptr()) }, 2 | 3 | 5 | 6)
}

// Only fixed local commands are allowed; requests cannot supply shell code.
const SERVICES: &str = r#"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); @(Get-Service | Select-Object -First 500 @{n='name';e={$_.Name}},@{n='display_name';e={$_.DisplayName}},@{n='status';e={$_.Status.ToString().ToLowerInvariant()}},@{n='start_type';e={$_.StartType.ToString()}}) | ConvertTo-Json -Compress -Depth 4"#;
const PROCESSES: &str = r#"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | Where-Object { $_.IDProcess -gt 0 } | Sort-Object PercentProcessorTime -Descending | Select-Object -First 300 @{n='pid';e={$_.IDProcess}},@{n='name';e={$_.Name}},@{n='user';e={'-'}},@{n='cpu';e={$_.PercentProcessorTime}},@{n='memory_mb';e={[math]::Round($_.WorkingSetPrivate/1MB,1)}}) | ConvertTo-Json -Compress -Depth 4"#;
const EVENTS: &str = r#"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); @(Get-WinEvent -FilterHashtable @{LogName='System'} -MaxEvents 100 | Select-Object @{n='time';e={$_.TimeCreated.ToString('s')}},@{n='source';e={$_.ProviderName}},@{n='level';e={if ($_.Level -le 2) {'error'} elseif ($_.Level -eq 3) {'warning'} else {'info'}}},@{n='message';e={ if ($_.Message -and $_.Message.Length -gt 2048) {$_.Message.Substring(0,2048)} else {$_.Message} }}) | ConvertTo-Json -Compress -Depth 4"#;
const SNAPSHOT: &str = r#"$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $os=Get-CimInstance Win32_OperatingSystem; $cs=Get-CimInstance Win32_ComputerSystem; $cpu=Get-CimInstance Win32_Processor | Select-Object -First 1; $disk=Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DeviceID -eq $os.SystemDrive } | Select-Object -First 1; $mem=0; $diskUsed=0; if ($os.TotalVisibleMemorySize -gt 0) {$mem=[math]::Round(100*(1-$os.FreePhysicalMemory/$os.TotalVisibleMemorySize),1)}; if ($disk.Size -gt 0) {$diskUsed=[math]::Round(100*(1-$disk.FreeSpace/$disk.Size),1)}; @{sysinfo=@{hostname=$env:COMPUTERNAME;username=$cs.UserName;platform='Windows';version=$os.Version;os_full=$os.Caption;cpu_name=$cpu.Name;cpu_cores=$cs.NumberOfLogicalProcessors;cpu_freq_ghz=[math]::Round($cpu.MaxClockSpeed/1000,2);memory_gb=[math]::Round($os.TotalVisibleMemorySize/1MB,2)};metrics=@{cpu_usage=$cpu.LoadPercentage;memory_usage=$mem;disk_usage=$diskUsed}} | ConvertTo-Json -Compress -Depth 5"#;

async fn collect_value(script: &'static str) -> Result<Value, String> {
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]);
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.kill_on_drop(true);
    // The console's default command timeout is 15 seconds.
    let output = timeout(Duration::from_secs(12), cmd.output()).await
        .map_err(|_| "collection_timeout".to_string())?
        .map_err(|_| "collection_unavailable".to_string())?;
    if !output.status.success() { return Err("collection_failed".into()); }
    if output.stdout.len() > 1_500_000 { return Err("collection_too_large".into()); }
    serde_json::from_slice(&output.stdout).map_err(|_| "invalid_collection_data".into())
}

async fn collect(script: &'static str, field: &'static str) -> Result<Value, String> {
    let value = collect_value(script).await?;
    let items = match value { Value::Array(a) => a, Value::Null => vec![], other => vec![other] };
    let mut result = serde_json::Map::new();
    result.insert(field.to_string(), Value::Array(items));
    Ok(Value::Object(result))
}

fn browse_files(payload: &Value) -> Result<Value, String> {
    let mode = Config::get_option("access-mode");
    if mode == "view" || (mode != "full" && Config::get_option("enable-file-transfer") == "N") {
        return Err("file_transfer_disabled".into());
    }
    let input = payload.get("path").and_then(Value::as_str).unwrap_or("");
    if input.is_empty() || input == "/" {
        let mask = unsafe { GetLogicalDrives() };
        if mask == 0 { return Err("drives_unavailable".into()); }
        let entries: Vec<Value> = (0..26).filter(|i| mask & (1 << i) != 0).filter(|i| {
            is_local_drive(&format!("{}:\\", (b'A' + *i as u8) as char))
        }).map(|i| {
            let name = format!("{}:", (b'A' + i as u8) as char);
            json!({"name": name, "path": format!("{}\\", name), "is_dir": true, "size": 0})
        }).collect();
        return Ok(json!({"path":"/", "parent":Value::Null, "entries":entries}));
    }
    let path = Path::new(input);
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) || input.starts_with("\\\\") {
        return Err("invalid_local_path".into());
    }
    let resolved = path.canonicalize().map_err(|_| "path_unavailable")?;
    let resolved_string = resolved.to_string_lossy();
    if !local_drive_root(&resolved_string).map(|root| is_local_drive(&root)).unwrap_or(false) {
        return Err("network_path_not_allowed".into());
    }
    let show_hidden = payload.get("show_hidden").and_then(Value::as_bool).unwrap_or(false);
    let display_path = |p: &Path| p.to_string_lossy().strip_prefix(r"\\?\").unwrap_or(&p.to_string_lossy()).to_string();
    let mut entries = Vec::new();
    for entry in std::fs::read_dir(&resolved).map_err(|_| "directory_unavailable")?.take(1000).flatten() {
        let metadata = match entry.metadata() { Ok(m) => m, Err(_) => continue };
        if !show_hidden && metadata.file_attributes() & 2 != 0 { continue; }
        entries.push(json!({"name":entry.file_name().to_string_lossy(),
            "path":display_path(&entry.path()), "is_dir":metadata.is_dir(),
            "size":if metadata.is_file() {metadata.len()} else {0}}));
    }
    entries.sort_by(|a, b| {
        b["is_dir"].as_bool().cmp(&a["is_dir"].as_bool())
            .then_with(|| a["name"].as_str().cmp(&b["name"].as_str()))
    });
    let display = display_path(&resolved);
    let parent = if display.len() == 3 && display.ends_with(":\\") {
        Some("/".to_string())
    } else {
        resolved.parent().map(display_path)
    };
    Ok(json!({"path":display, "parent":parent, "entries":entries}))
}

async fn dispatch(kind: &str, payload: Value) -> Result<Value, String> {
    match kind {
        "services.list" => collect(SERVICES, "services").await,
        "processes.list" => collect(PROCESSES, "processes").await,
        "events.list" => collect(EVENTS, "events").await,
        "inventory.snapshot" => collect_value(SNAPSHOT).await,
        "files.browse" => tokio::task::spawn_blocking(move || browse_files(&payload)).await.map_err(|_| "collection_failed")?,
        _ => Err("unsupported_command".into()),
    }
}

async fn run_connection() -> Result<(), String> {
    // Wait until RustDesk itself has established an identity; this worker must
    // never create a new key while the service is starting up.
    let (secret, _) = Config::get_existing_key_pair().ok_or("device_key_not_ready")?;
    let id = Config::get_id();
    if id.len() < 3 || id.len() > 64 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') {
        return Err("device_id_not_ready".into());
    }
    let url = format!("{}/ws/bd-signal?auth=rdkey-v1&device_id={}", CONSOLE_ORIGIN, id);
    let mut ws = WsFramedStream::new_rdgen_strict(&url, 10000).await.map_err(|_| "connect_failed")?;
    ws.set_max_packet_length(65536);
    let first = ws.next_timeout(10000).await.ok_or("challenge_timeout")?.map_err(|_| "challenge_failed")?;
    let msg: Value = serde_json::from_slice(&first).map_err(|_| "invalid_challenge")?;
    if msg.get("type").and_then(Value::as_str) != Some("auth_challenge") { return Err("invalid_challenge".into()); }
    let challenge = msg.get("challenge").and_then(Value::as_str).ok_or("invalid_challenge")?;
    if STANDARD.decode(challenge).map_err(|_| "invalid_challenge")?.len() != 32 { return Err("invalid_challenge".into()); }
    let key = sign::SecretKey::from_slice(&secret).ok_or("device_key_not_ready")?;
    let message = format!("{}{}\n{}", CHALLENGE_PREFIX, id, challenge);
    let signature = sign::sign_detached(message.as_bytes(), &key);
    let proof = json!({"type":"auth_proof", "signature":STANDARD.encode(signature.to_bytes())});
    ws.send_raw(serde_json::to_vec(&proof).map_err(|_| "encode_failed")?).await.map_err(|_| "send_failed")?;
    let welcome = ws.next_timeout(10000).await.ok_or("auth_timeout")?.map_err(|_| "auth_failed")?;
    let welcome: Value = serde_json::from_slice(&welcome).map_err(|_| "auth_failed")?;
    if welcome.get("type").and_then(Value::as_str) != Some("welcome") { return Err("auth_failed".into()); }
    loop {
        let bytes = ws.next_timeout(90000).await.ok_or("connection_closed")?.map_err(|_| "connection_failed")?;
        let msg: Value = match serde_json::from_slice(&bytes) { Ok(v) => v, Err(_) => continue };
        let request_id = match msg.get("request_id").and_then(Value::as_str) {
            Some(v) if v.len() <= 80 => v, _ => continue,
        };
        let kind = msg.get("type").and_then(Value::as_str).unwrap_or("");
        let reply = match dispatch(kind, msg.get("payload").cloned().unwrap_or(Value::Null)).await {
            Ok(data) => json!({"type":"command_response", "request_id":request_id, "ok":true, "data":data}),
            Err(error) => json!({"type":"command_response", "request_id":request_id, "ok":false, "error":error}),
        };
        ws.send_raw(serde_json::to_vec(&reply).map_err(|_| "encode_failed")?).await.map_err(|_| "send_failed")?;
    }
}

pub async fn run() {
    loop {
        if let Err(error) = run_connection().await { log::warn!("[inventory] {}", error); }
        sleep(Duration::from_secs(30)).await;
    }
}
