#!/bin/bash
# Destructive package lifecycle test: only on a disposable GitHub Linux runner.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_OS:-} == Linux ]] || {
  echo 'This test requires a disposable GitHub Actions Linux runner.' >&2; exit 1;
}
cd "$(dirname "$0")/.."
mapfile -t packages < <(find dist -maxdepth 1 -name '*.deb')
[[ ${#packages[@]} == 1 ]]
package=$(realpath "${packages[0]}")
unit=hrx-fancontrol.service
# Even accidental starts during the remaining test cannot run IPMI.
sudo mkdir -p /etc/systemd/system/hrx-fancontrol.service.d
sudo tee /etc/systemd/system/hrx-fancontrol.service.d/package-test.conf >/dev/null <<'EOF'
[Service]
Type=simple
NotifyAccess=none
ExecStart=
ExecStart=/bin/sleep infinity
ExecStopPost=
WatchdogSec=0
EOF
sudo systemctl daemon-reload
sudo apt-get update -qq
sudo apt-get install -y "$package"
[[ $(systemctl is-active "$unit" || true) != active ]]
[[ $(systemctl is-enabled "$unit" || true) == disabled ]]
# Native CLI must work without a virtualenv, network pip, or BMC.
/usr/bin/hrx-fancontrol --once --input examples/hr650x-observed-sensors.txt --json --no-log >/tmp/hrx-package-replay.json
python3 - <<'PY'
import json
with open('/tmp/hrx-package-replay.json') as f:
    result = json.load(f)
assert result['mode'] == 'dry-run' and result['applied'] is False
PY
sudo mkdir -p /etc/hrx-fancontrol /var/lib/hrx-fancontrol /var/log/hrx-fancontrol
printf 'logging:\n  directory: /var/log/hrx-fancontrol\n  interval_seconds: 90\n' | sudo tee /etc/hrx-fancontrol/config.yaml >/dev/null
for path in /etc/hrx-fancontrol/calibration.json /var/lib/hrx-fancontrol/owner.json /var/lib/hrx-fancontrol/service-mode.json /var/log/hrx-fancontrol/preserved.jsonl; do
  printf 'preserve this file\n' | sudo tee "$path" >/dev/null
done
sudo sha256sum /etc/hrx-fancontrol/*.yaml /etc/hrx-fancontrol/*.json /var/lib/hrx-fancontrol/*.json /var/log/hrx-fancontrol/preserved.jsonl >/tmp/hrx-package-data.sha256
# Real version upgrades also change the default config to exercise conffile retention.
fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT
base_version=$(dpkg-deb -f "$package" Version)
iteration=0
for enabled in yes no; do
  if [[ $enabled == yes ]]; then sudo systemctl enable "$unit"; else sudo systemctl disable "$unit"; fi
  for active in yes no; do
    if [[ $active == yes ]]; then sudo systemctl start "$unit"; else sudo systemctl stop "$unit"; fi
    iteration=$((iteration + 1))
    tree="$fixture/package-$iteration"
    dpkg-deb --raw-extract "$package" "$tree"
    sed -i "s/^Version:.*/Version: $base_version+test$iteration/" "$tree/DEBIAN/control"
    printf 'logging:\n  directory: /var/log/hrx-fancontrol\n  interval_seconds: %s\n' "$iteration" >"$tree/etc/hrx-fancontrol/config.yaml"
    dpkg-deb --root-owner-group --build "$tree" "$fixture/upgrade.deb" >/dev/null
    sudo dpkg --force-confold -i "$fixture/upgrade.deb"
    if [[ $active == yes ]]; then systemctl is-active --quiet "$unit"; else [[ $(systemctl is-active "$unit" || true) != active ]]; fi
    if [[ $enabled == yes ]]; then systemctl is-enabled --quiet "$unit"; else [[ $(systemctl is-enabled "$unit" || true) == disabled ]]; fi
    sudo sha256sum --check /tmp/hrx-package-data.sha256
  done
done
sudo systemctl enable --now "$unit"
sudo apt-get remove -y hrx-fancontrol
[[ $(systemctl is-active "$unit" || true) != active ]]
sudo sha256sum --check /tmp/hrx-package-data.sha256
sudo apt-get purge -y hrx-fancontrol
[[ ! -e /etc/systemd/system/multi-user.target.wants/hrx-fancontrol.service && ! -L /etc/systemd/system/multi-user.target.wants/hrx-fancontrol.service ]]
# dpkg purges its config.yaml; machine-generated files remain.
for path in /etc/hrx-fancontrol/calibration.json /var/lib/hrx-fancontrol/owner.json /var/lib/hrx-fancontrol/service-mode.json /var/log/hrx-fancontrol/preserved.jsonl; do
  sudo test -f "$path"
done
printf 'Native install, CLI replay, four upgrade states, remove and purge passed.\n'
