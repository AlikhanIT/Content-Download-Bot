#!/bin/sh

while true
do
  nordvpn connect --group P2P
  sleep 1800  # 30 минут
  nordvpn disconnect
done
