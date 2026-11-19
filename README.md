# RDGen, a RustDesk client generator to use with your self-hosted RustDesk server

The client generator is currently hosted [here](https://rdgen.crayoneater.org).
If you would like to host the generator yourself, see [here](setup.md)

## rustdesk api server integration

The rdgen client generator is integrated into my fork of the [rustdesk-api](https://github.com/bryangerlach/rustdesk-api). This is currently a work in progress, and might have bugs.

## rdgen local build alternative

If you would prefer to build rustdesk on your own computer, check out the [DVForge](https://github.com/VenimK/DVForge) project.

## Features

- Embed server and key into client
- Custom app name
- Custom icon/logo
- Custom theme colors (accent, light/dark surfaces, ID/“me” color)
- Set default settings for the client
- Support for rustdesk advanced settings (https://rustdesk.com/docs/en/self-host/client-configuration/advanced-settings/)

## Generate RustDesk clients from command line instead of using a web browser

Save your configuration from the rdgen web interface, or generate your own, then use that json file with [@AlekseyLapunov's rdgen-cli](https://github.com/AlekseyLapunov/rdgen-cli) to build from the command line on Windows, Linux, or MacOS like this: `python rdgen-cli -f my_config.json --set-version 1.4.5 --set-platform windows -s https://rdgen.crayoneater.org`

## Notes

- Icons should be square (256x256 recommended)
- Avoid special characters or non-English characters in app name and file name
- Build time is currently 30 - 45 minutes
- Permissions and security settings are written to `override-settings`, so they also apply to machines that already have a saved RustDesk configuration and cannot be changed by the client. The old Default/Override selector for permissions was removed.
- A permanent password baked into a client is always accepted, even if the machine already has a local permanent password. Setting one also selects permanent-password verification.

## Windows service installation

For a Windows x64 client that accepts incoming connections, leave installation
enabled and download the MSI from the generation results. Run it as an
administrator. The package installs the Windows service so the client can accept
connections at the sign-in screen before a user logs in. The installer does not
open the client window or add a tray shortcut at Windows sign-in. The client
remains available from the Start menu.

For managed installation without launching the client interface, run this from
an elevated shell. Substitute the downloaded MSI filename.

```powershell
msiexec.exe /i ".\client_v1.5.3.msi" /qn /norestart
```

Confirm that the service shows as running in Windows Services, then test from a
second device after signing out and after restarting the host.

### Enter text at the Windows sign-in screen

Connect from the desktop controller with keyboard input enabled. Click the
remote username field, copy the username on the controller, then choose
**Control Actions → Send clipboard keystrokes** in the remote session toolbar.
Repeat for the password field and submit Windows sign-in manually. This uses
RustDesk's keyboard input for the focused field.

### Signed online updates for the Windows service

Create separate Ed25519 key pairs and random 32-byte hexadecimal access tokens
for Host and Admin. Keep the signing keys with the release operator. Set the
following variables in the RDGen deployment.

| Variable | Value |
| --- | --- |
| `RDGEN_UPDATE_ORIGIN` | HTTPS origin for updates, including a port if needed |
| `RDGEN_UPDATE_ROOT` | Persistent directory for signed releases |
| `RDGEN_UPDATE_HOST_ACCESS`, `RDGEN_UPDATE_ADMIN_ACCESS` | Different 64-character lowercase hexadecimal access tokens |
| `RDGEN_UPDATE_HOST_PUBLIC_KEY`, `RDGEN_UPDATE_ADMIN_PUBLIC_KEY` | Different Base64-encoded raw Ed25519 public keys |
| `RDGEN_UPDATE_BUILD_TOKEN` | Separate secret required to generate clients that use these channels |

Set **Windows automatic updates** to the matching channel on an installable
Windows x64 build with RustDesk 1.5.3. Enter the build token at submission.
Leave **Application version** blank for the first installation. To release
another build from the same RustDesk tag, set a higher application version such
as `1.5.3+001`. Keep the app name identical between releases so Windows
Installer upgrades the existing product.

On the signing machine, set the same channel environment and a local
`RDGEN_UPDATE_ROOT`. Sign the MSI from the corresponding build.

```text
python manage.py publish_windows_update host 1.5.3+001 path/to/host.msi --signing-key path/to/host-ed25519.pem
```

Transfer the complete version directory to that channel on the server before
replacing its `latest` file with the signed release's pointer. The service
uses HTTPS and verifies the channel's signature and the installer's SHA-256
before installing. RustDesk defers installation while a remote session is active.
Avoid publishing a generated package containing a shared preset password to a
public release location. Keep channel access URLs out of access logs and restrict
the update endpoint to the devices that need it where your network permits.
