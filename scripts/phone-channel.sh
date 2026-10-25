#!/bin/bash
# Поднимает мобильный канал через USB-тетеринг телефона и привязывает tinyproxy.
# Использование: sudo phone-channel [интерфейс]
set -u
LOG=/var/log/mobile_channel.log
log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG" >/dev/null; }

IF="${1:-}"
if [ -z "$IF" ]; then
  for cand in $(ip -brief addr | awk '/^enx|^usb/{print $1}'); do
    ip4=$(ip -4 -brief addr show "$cand" | awk '{print $3}' | cut -d/ -f1)
    case "$ip4" in
      192.168.8.*|192.168.10.*|"") continue ;;
      10.*|192.168.42.*|172.2*) IF="$cand"; break ;;
      *) IF="${IF:-$cand}" ;;
    esac
  done
fi
[ -z "$IF" ] && { log "интерфейс терингa не найден"; echo "NO_IFACE"; exit 1; }

IP=$(ip -4 -brief addr show "$IF" | awk '{print $3}' | cut -d/ -f1)
GW=$(ip route show dev "$IF" | awk '/^default/{print $3; exit}')
[ -z "$GW" ] && GW=$(ip route show dev "$IF" | awk '/proto (dhcp|kernel)/ && /\/24|_proto/{print $1; exit}')
[ -z "$GW" ] && { log "не найден шлюз для $IF"; echo "NO_GW"; exit 1; }

log "интерфейс $IF, адрес $IP, шлюз $GW"

# мобильный канал — только в таблицу 300, из main дефолт убираем
ip route del default via "$GW" dev "$IF" 2>/dev/null
ip route replace default via "$GW" dev "$IF" table 300
ip rule add from "$IP" table 300 priority 890 2>/dev/null

iptables -t mangle -C POSTROUTING -o "$IF" -j TTL --ttl-set 65 2>/dev/null || \
  iptables -t mangle -A POSTROUTING -o "$IF" -j TTL --ttl-set 65

# tinyproxy: слушает локально и для docker-сети, выходит с адреса телефона
sed -i -E "s/^Bind .*/Bind $IP/" /etc/tinyproxy/tinyproxy.conf
grep -q "Allow 172.16.0.0/12" /etc/tinyproxy/tinyproxy.conf || \
  sed -i "s|^Allow 127.0.0.1|Allow 127.0.0.1\nAllow 172.16.0.0/12|" /etc/tinyproxy/tinyproxy.conf
systemctl restart tinyproxy

sleep 2
OUT=$(curl -s -m 12 -x http://127.0.0.1:8888 https://api.ipify.org)
if [ -n "$OUT" ]; then
  log "канал поднят: $IF, внешний IP $OUT"
  echo "OK $OUT"
else
  log "прокси не отвечает, проверь теринг"
  echo "NO_INTERNET"
  exit 1
fi
