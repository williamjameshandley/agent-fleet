# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=agent-fleet
pkgver=0.3.0.r1786185191.g21f0301
pkgrel=1
pkgdesc='Awareness and one-keypress switching for a fleet of terminal AI-agent sessions in tmux'
arch=('x86_64')
url='https://github.com/williamjameshandley/agent-fleet'
license=('MIT')
options=('!debug')
depends=('alan>=1:3.0.0.a1' python python-libtmux python-watchfiles jupyter-console openai-codex tmux fzf openssh openbsd-netcat curl procps-ng libvterm)
optdepends=(
    'ghostty: workstation viewer terminals'
    'i3-wm: workstation layout and focus control'
    'jq: workstation launcher window discovery'
    'alan-home-satellite: voice events from the Alan Home speech pipeline'
    'python-gobject: Alan composer interface'
    'xdotool: Alan destination focus restoration'
)
source=()
sha256sums=()

pkgver() {
  printf '0.3.0.r%s.g%s\n' \
    "$(git -C "$startdir" show -s --format=%ct HEAD)" \
    "$(git -C "$startdir" rev-parse --short=7 HEAD)"
}

package() {
  for script in fleet-muster fleet-viewer fleet-view fleet-deck fleet-office fleet-commander; do
    install -Dm755 "$startdir/$script" "$pkgdir/usr/bin/$script"
  done
  install -d "$pkgdir/usr/lib/agent-fleet"
  printf '#!/usr/bin/python\nfrom agent_fleet.ui_process import main\nmain()\n' \
    > "$pkgdir/usr/lib/agent-fleet/ui"
  chmod 755 "$pkgdir/usr/lib/agent-fleet/ui"
  printf '#!/usr/bin/python\nfrom agent_fleet.authority import main\nmain()\n' \
    > "$pkgdir/usr/lib/agent-fleet/action"
  chmod 755 "$pkgdir/usr/lib/agent-fleet/action"
  install -Dm755 "$startdir/fleet-open" "$pkgdir/usr/lib/agent-fleet/fleet-open"
  install -Dm755 "$startdir/fleet-present" "$pkgdir/usr/lib/agent-fleet/fleet-present"
  install -Dm755 "$startdir/fleet-status" "$pkgdir/usr/lib/agent-fleet/fleet-status"
  install -Dm755 "$startdir/fleet-switch" "$pkgdir/usr/lib/agent-fleet/fleet-switch"
  install -Dm755 "$startdir/fleet-tmux" "$pkgdir/usr/lib/agent-fleet/fleet-tmux"
  cc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror \
    "$startdir/fleet-preview.c" -o "$pkgdir/usr/lib/agent-fleet/fleet-preview" -lvterm
  local purelib="$pkgdir$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
  install -d "$purelib/agent_fleet"
  install -m644 "$startdir"/agent_fleet/*.py "$purelib/agent_fleet/"
  install -d "$purelib/alan_composer"
  install -m644 "$startdir"/alan_composer/*.py "$purelib/alan_composer/"
  install -Dm755 "$startdir/alan-composer" "$pkgdir/usr/bin/alan-composer"
  install -Dm644 "$startdir/alan-composer.service" "$pkgdir/usr/lib/systemd/user/alan-composer.service"
  install -Dm644 "$startdir/fleet.service" "$pkgdir/usr/lib/systemd/user/fleet.service"
  install -Dm644 "$startdir/fleet-quota.service" "$pkgdir/usr/lib/systemd/user/fleet-quota.service"
  install -Dm644 "$startdir/fleet-quota.timer" "$pkgdir/usr/lib/systemd/user/fleet-quota.timer"
  install -Dm644 "$startdir/presets/commander.md" "$pkgdir/usr/share/alan/presets/commander.md"
  install -Dm644 "$startdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
