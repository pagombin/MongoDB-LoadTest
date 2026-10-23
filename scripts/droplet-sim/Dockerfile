FROM ubuntu:24.04
COPY debs/ /tmp/debs/
RUN dpkg -i /tmp/debs/*.deb >/dev/null 2>&1; dpkg -i /tmp/debs/*.deb >/dev/null 2>&1; dpkg --configure -a; test -x /usr/sbin/cron; rm -rf /tmp/debs \
 && useradd -m -s /bin/bash bench \
 && printf '#!/bin/sh\nrm -f /run/crond.reboot /run/crond.pid\ncron\nexec sleep infinity\n' > /init.sh && chmod +x /init.sh
CMD ["/init.sh"]
