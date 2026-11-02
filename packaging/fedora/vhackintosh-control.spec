Name:           vhackintosh-control
Version:        %{pkg_version}
Release:        1%{?dist}
Summary:        vHackintosh Control and macOS VM host manager
License:        MIT AND GPL-3.0-or-later AND BSD-3-Clause
BuildArch:      x86_64
Requires:       systemd
Requires:       python3 >= 3.12
Requires:       openssl
Requires:       curl
Requires:       kbd
Requires:       sway
Requires:       swaybg
Requires:       seatd
Requires:       dbus-x11
Requires:       pipewire
Requires:       pipewire-pulseaudio
Requires:       wireplumber
Requires:       vulkan-loader
Requires:       mesa-vulkan-drivers
Requires:       gtk3
Requires:       libepoxy
Requires:       pixman
Requires:       libslirp
Requires:       libpng
Requires:       libjpeg-turbo
Requires:       alsa-lib
Requires:       pulseaudio-libs
Requires:       zstd
Requires:       ncurses-libs
Requires:       bzip2-libs
Requires:       libusb1
Requires:       numactl-libs
Requires:       glib2

%description
Installs the vHackintosh HTTPS dashboard, an HTTP-to-HTTPS redirect, the pinned
OpenCorePkg macserial utility, privileged local agent, and shared Wayland
display session for macOS virtual machines. The Reims vGPU runtime is downloaded
from the configured GitHub fork after first sign-in.

%prep

%build

%install
mkdir -p %{buildroot}
cp -a %{payload_dir}/. %{buildroot}/

%post
systemd-sysusers /usr/lib/sysusers.d/vhackintosh-web.conf || :
systemd-tmpfiles --create /usr/lib/tmpfiles.d/vhackintosh-web.conf || :
install -d -m 0700 /root/.config/vhackintosh /root/.config/reims-ctl /root/.local/share/vhackintosh || :
systemctl daemon-reload || :
systemctl enable seatd.service >/dev/null 2>&1 || :
systemctl start seatd.service >/dev/null 2>&1 || :
systemctl enable vhackintosh-agent.service vhackintosh-web.service >/dev/null 2>&1 || :
systemctl start vhackintosh-agent.service vhackintosh-web.service >/dev/null 2>&1 || :
systemctl enable vhackintosh-http-redirect.service >/dev/null 2>&1 || :
systemctl restart vhackintosh-http-redirect.service >/dev/null 2>&1 || systemctl start vhackintosh-http-redirect.service >/dev/null 2>&1 || :

%preun
if [ "$1" -eq 0 ]; then
    systemctl disable --now vhackintosh-http-redirect.service vhackintosh-web.service vhackintosh-agent.service vhackintosh-display.service >/dev/null 2>&1 || :
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :

%files
/opt/vhackintosh/web/current
/opt/vhackintosh/resources/macserial
/usr/lib/systemd/system/vhackintosh-agent.service
/usr/lib/systemd/system/vhackintosh-display.service
/usr/lib/systemd/system/vhackintosh-vm@.service
/usr/lib/systemd/system/vhackintosh-web.service
/usr/lib/systemd/system/vhackintosh-http-redirect.service
%config(noreplace) /etc/vhackintosh/sway.conf
/usr/lib/sysusers.d/vhackintosh-web.conf
/usr/lib/tmpfiles.d/vhackintosh-web.conf
/usr/local/bin/vhackintosh-web-bootstrap
/usr/local/bin/vhackintosh-waysession
/usr/local/bin/vhackintosh-wait-wayland

%changelog
* Mon Nov 02 2026 Felipe Abreu <felipeab10@users.noreply.github.com> - 1.0.0
- Initial Fedora package
