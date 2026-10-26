ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}
COPY sources.list /etc/apt/sources.list
COPY cockpit.deb /tmp/cockpit.deb
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       /tmp/cockpit.deb ca-certificates libgl1-mesa-dri libglx-mesa0 libnss3 \
       gsettings-desktop-schemas \
    && mkdir /out \
    && dpkg-query -W > /out/packages.tsv \
    && tar -czf /out/runtime.tar.gz -C / \
       usr/lib usr/lib64 usr/libexec usr/share usr/bin/cockpit-tools \
       usr/bin/cockpit-cliproxy usr/bin/secret-tool usr/bin/bwrap \
       usr/bin/xdg-dbus-proxy etc/ld.so.cache
