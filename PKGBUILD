# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=agent-fleet
pkgver=0.1.0.r58
pkgrel=1
pkgdesc='Awareness and one-keypress switching for a fleet of terminal AI-agent sessions in tmux'
arch=('any')
url='https://github.com/williamjameshandley/agent-fleet'
license=('MIT')
depends=(python tmux otf-font-awesome)
optdepends=(
    'openssh: polling and remote shadow windows (the flagship)'
    'fzf: the muster column (the flagship)'
    'curl: selection pushes to the muster column (the flagship)'
    'procps-ng: ps maps agent process trees; watch drives the muster window'
    'python-openwakeword: wake-dryrun harness (the mic machine)'
    'python-sounddevice: wake-dryrun harness (the mic machine)'
    'python-numpy: wake-dryrun harness (the mic machine)'
)
source=()
sha256sums=()

pkgver() {
  cd "$startdir"
  local version hash
  version="0.1.0.r$(git rev-list --count HEAD)"
  if git diff --quiet && git diff --cached --quiet; then
    printf '%s\n' "$version"
  else
    hash=$(sha256sum fleet fleet-usage tmux.conf wake-dryrun wake-dryrun.service LICENSE \
      | sha256sum | cut -c1-8)
    printf '%s.d%s\n' "$version" "$hash"
  fi
}

package() {
  install -Dm755 "$startdir/fleet" "$pkgdir/usr/bin/fleet"
  install -Dm755 "$startdir/fleet-usage" "$pkgdir/usr/bin/fleet-usage"
  install -Dm644 "$startdir/fleet" \
    "$pkgdir$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/fleet.py"
  install -Dm644 "$startdir/tmux.conf" "$pkgdir/usr/share/agent-fleet/tmux.conf"
  install -Dm755 "$startdir/wake-dryrun" "$pkgdir/usr/lib/agent-fleet/wake-dryrun"
  install -Dm644 "$startdir/wake-dryrun.service" "$pkgdir/usr/lib/systemd/user/wake-dryrun.service"
  install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
