# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=agent-fleet
pkgver=0.2.0.r90
pkgrel=1
pkgdesc='Awareness and one-keypress switching for a fleet of terminal AI-agent sessions in tmux'
arch=('x86_64')
url='https://github.com/williamjameshandley/agent-fleet'
license=('MIT')
options=('!debug')
depends=(python python-libtmux python-watchfiles tmux fzf openssh curl procps-ng libvterm)
optdepends=(
    'ghostty: workstation viewer terminals'
    'i3-wm: workstation layout and focus control'
    'jq: workstation launcher window discovery'
    'python-openwakeword: wake-dryrun harness (the mic machine)'
    'python-sounddevice: wake-dryrun harness (the mic machine)'
    'python-numpy: wake-dryrun harness (the mic machine)'
)
source=()
sha256sums=()

pkgver() {
  cd "$startdir"
  local version hash
  version="0.2.0.r$(git rev-list --count HEAD)"
  if git diff --quiet && git diff --cached --quiet; then
    printf '%s\n' "$version"
  else
    hash=$(sha256sum fleet fleet-next fleet-preview.c fleet-muster fleet-viewer fleet-view \
      fleet-deck fleet-office fleet-commander fleet_next/*.py fleet-usage tmux.conf \
      fleet-next.service fleet-quota.service fleet-quota.timer \
      wake-dryrun wake-dryrun.service LICENSE \
      | sha256sum | cut -c1-8)
    printf '%s.d%s\n' "$version" "$hash"
  fi
}

package() {
  install -Dm755 "$startdir/fleet" "$pkgdir/usr/lib/agent-fleet/fleet-legacy"
  install -Dm755 "$startdir/fleet-next" "$pkgdir/usr/bin/fleet-next"
  for script in fleet-muster fleet-viewer fleet-view fleet-deck fleet-office fleet-commander; do
    install -Dm755 "$startdir/$script" "$pkgdir/usr/bin/$script"
  done
  install -Dm755 "$startdir/fleet-usage" "$pkgdir/usr/bin/fleet-usage"
  cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror \
    "$startdir/fleet-preview.c" -o "$pkgdir/usr/lib/agent-fleet/fleet-preview" -lvterm
  local purelib="$pkgdir$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  install -d "$purelib/fleet_next"
  install -m644 "$startdir"/fleet_next/*.py "$purelib/fleet_next/"
  install -Dm644 "$startdir/tmux.conf" "$pkgdir/usr/share/agent-fleet/tmux.conf"
  install -Dm755 "$startdir/wake-dryrun" "$pkgdir/usr/lib/agent-fleet/wake-dryrun"
  install -Dm644 "$startdir/wake-dryrun.service" "$pkgdir/usr/lib/systemd/user/wake-dryrun.service"
  install -Dm644 "$startdir/fleet-next.service" "$pkgdir/usr/lib/systemd/user/fleet-next.service"
  install -Dm644 "$startdir/fleet-quota.service" "$pkgdir/usr/lib/systemd/user/fleet-quota.service"
  install -Dm644 "$startdir/fleet-quota.timer" "$pkgdir/usr/lib/systemd/user/fleet-quota.timer"
  install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
