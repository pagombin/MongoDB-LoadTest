# Simulated droplet image (development only)

Ubuntu 24.04 with only `curl` and `cron` added (both are present on a stock DigitalOcean Ubuntu droplet), a normal user `bench`, and no `sudo`. The init script clears `/run/crond.reboot`, as a real boot does because `/run` is a tmpfs, so `@reboot` crontab entries fire after `docker restart`.

The packages are copied in as `.deb` files so the image builds without network access inside the build:

```
mkdir -p debs && cd debs
apt-get download cron cron-daemon-common adduser systemd-standalone-sysusers sensible-utils curl libcurl4t64 ca-certificates openssl libnghttp2-14 libpsl5t64 librtmp1 libssh-4 libbrotli1 libldap2 libsasl2-2 libsasl2-modules-db libgssapi-krb5-2 libkrb5-3 libk5crypto3 libkrb5support0 libkeyutils1 libcom-err2 procps libproc2-0 iproute2 libbpf1 libelf1t64 libmnl0 libxtables12 libcap2-bin libatm1t64
cd .. && docker build -t ballast-droplet-sim .
```

Then run `scripts/simulate-fresh-host.sh dist/ballast-<version>-linux-amd64.tar.gz` with the test replica set from `scripts/dev-replset.sh` running.
