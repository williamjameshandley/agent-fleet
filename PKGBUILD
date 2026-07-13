# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=agent-fleet
pkgver=0.1.0.r45.g3cfba18.dirty
pkgrel=1
pkgdesc='Awareness and one-keypress switching for a fleet of terminal AI-agent sessions in tmux'
arch=('any')
url='https://github.com/williamjameshandley/agent-fleet'
license=('MIT')
# Ships (agent hosts) only run the hook. Everything else is role-tagged.
depends=(tmux jq)
optdepends=(
    'python: fleet itself (the flagship)'
    'openssh: polling and remote shadow windows (the flagship)'
    'fzf: the muster column (the flagship)'
    'curl: selection pushes to the muster column (the flagship)'
    'procps-ng: watch drives the muster window (the flagship)'
    'python-openwakeword: wake-dryrun harness (the mic machine)'
    'python-sounddevice: wake-dryrun harness (the mic machine)'
    'python-numpy: wake-dryrun harness (the mic machine)'
)
source=()
sha256sums=()

pkgver() {
  cd "$startdir"
  git describe --long --tags --always --dirty=.dirty \
    | sed 's/^v//; s/\([^-]*-g\)/r\1/; s/-/./g'
}

package() {
  install -Dm755 "$startdir/fleet" "$pkgdir/usr/bin/fleet"
  install -Dm755 "$startdir/hook" "$pkgdir/usr/lib/agent-fleet/hook"
  install -Dm755 "$startdir/wake-dryrun" "$pkgdir/usr/lib/agent-fleet/wake-dryrun"
  install -Dm644 "$startdir/wake-dryrun.service" "$pkgdir/usr/lib/systemd/user/wake-dryrun.service"
}
