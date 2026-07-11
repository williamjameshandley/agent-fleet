# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=claude-fleet
pkgver=0.1.0.r2.g25cd90a.dirty
pkgrel=1
pkgdesc='Awareness panel and one-keypress switching for terminal AI-agent sessions in tmux'
arch=('any')
url='https://github.com/williamjameshandley/claude-fleet'
license=('MIT')
depends=(python tmux jq fzf openssh)
# Contents are read directly from $startdir (see alan-home for the pattern:
# empty source=() is the lean answer; the install lines are the explicit map
# from repo layout to installed contract).
source=()
sha256sums=()

pkgver() {
  cd "$startdir"
  git describe --long --tags --always --dirty=.dirty \
    | sed 's/^v//; s/\([^-]*-g\)/r\1/; s/-/./g'
}

package() {
  install -Dm755 "$startdir/fleet" "$pkgdir/usr/bin/fleet"
  install -Dm755 "$startdir/hook" "$pkgdir/usr/lib/claude-fleet/hook"
}
