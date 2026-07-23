# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=agent-fleet
pkgver=0.3.0.r1784748237.g01936d4
pkgrel=1
pkgdesc='Awareness and one-keypress switching for a fleet of terminal AI-agent sessions in tmux'
arch=('x86_64')
url='https://github.com/williamjameshandley/agent-fleet'
license=('MIT')
options=('!debug')
depends=(python python-libtmux python-watchfiles jupyter-console openai-codex tmux fzf openssh curl procps-ng libvterm)
optdepends=(
    'ghostty: workstation viewer terminals'
    'i3-wm: workstation layout and focus control'
    'jq: workstation launcher window discovery'
    'python-openwakeword: wake-dryrun harness (the mic machine)'
    'python-sounddevice: wake-dryrun harness (the mic machine)'
    'python-numpy: wake-dryrun harness (the mic machine)'
    'python-gobject: Alan composer interface (the mic machine)'
    'xdotool: Alan destination focus restoration (the mic machine)'
    'ffmpeg: Alan ambient FLAC archive (the mic machine)'
)
source=()
sha256sums=()

pkgver() {
  printf '0.3.0.r%s.g%s\n' \
    "$(git -C "$startdir" show -s --format=%ct HEAD)" \
    "$(git -C "$startdir" rev-parse --short=7 HEAD)"
}

package() {
  install -Dm755 "$startdir/fleet-next" "$pkgdir/usr/bin/fleet-next"
  install -Dm644 "$startdir/alan-socket" "$pkgdir/etc/agent-fleet/alan-socket"
  for script in fleet-muster fleet-viewer fleet-view fleet-deck fleet-office fleet-commander fleet-snapshot; do
    install -Dm755 "$startdir/$script" "$pkgdir/usr/bin/$script"
  done
  install -Dm755 "$startdir/fleet-usage" "$pkgdir/usr/bin/fleet-usage"
  install -d "$pkgdir/usr/lib/agent-fleet"
  cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror \
    "$startdir/fleet-preview.c" -o "$pkgdir/usr/lib/agent-fleet/fleet-preview" -lvterm
  local purelib="$pkgdir$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  install -d "$purelib/fleet_next"
  install -m644 "$startdir"/fleet_next/*.py "$purelib/fleet_next/"
  install -d "$purelib/alan_composer"
  install -m644 "$startdir"/alan_composer/*.py "$purelib/alan_composer/"
  install -Dm755 "$startdir/wake-dryrun" "$pkgdir/usr/lib/agent-fleet/wake-dryrun"
  install -Dm644 "$startdir/wake-dryrun.service" "$pkgdir/usr/lib/systemd/user/wake-dryrun.service"
  install -Dm755 "$startdir/alan-composer" "$pkgdir/usr/bin/alan-composer"
  install -Dm644 "$startdir/alan-composer.service" "$pkgdir/usr/lib/systemd/user/alan-composer.service"
  install -Dm644 "$startdir/fleet-next.service" "$pkgdir/usr/lib/systemd/user/fleet-next.service"
  install -Dm644 "$startdir/fleet-quota.service" "$pkgdir/usr/lib/systemd/user/fleet-quota.service"
  install -Dm644 "$startdir/fleet-quota.timer" "$pkgdir/usr/lib/systemd/user/fleet-quota.timer"
  install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
