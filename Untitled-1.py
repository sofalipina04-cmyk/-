#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
CONFIG_DIR="/etc/ddos-shield"
LOG_FILE="/var/log/ddos-shield.log"
BLACKLIST_NAME="ddos_blacklist"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a $LOG_FILE
}

print_header() {
    clear
    echo "============================================="
    echo "    DDoS Shield v1.0 - Sistema zashchity    "
    echo "============================================="
    echo ""
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}Oshibka: Etot skript dolzhen zapuskatsya ot root${NC}"
        exit 1
    fi
}

install_dependencies() {
    log "Ustanovka zavisimostey..."
    apt update -y
    apt install -y iptables iptables-persistent fail2ban ipset hping3 tcpdump iftop net-tools
    log "Zavisimosti ustanovleny"
}

setup_directories() {
    mkdir -p $CONFIG_DIR
    touch $LOG_FILE
    chmod 644 $LOG_FILE
}

setup_ipset() {
    log "Nastroyka ipset..."
    if ipset list $BLACKLIST_NAME &>/dev/null; then
        ipset flush $BLACKLIST_NAME
    else
        ipset create $BLACKLIST_NAME hash:ip timeout 3600
    fi
    if ! iptables -C INPUT -m set --match-set $BLACKLIST_NAME src -j DROP 2>/dev/null; then
        iptables -I INPUT -m set --match-set $BLACKLIST_NAME src -j DROP
    fi
    ipset save > /etc/ipset.conf
    log "ipset nastroen. Sozdan nabor: $BLACKLIST_NAME"
}

setup_iptables() {
    log "Nastroyka iptables..."
    iptables -F INPUT
    iptables -F FORWARD
    iptables -F OUTPUT
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT ACCEPT
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT -p tcp --syn -m limit --limit 1/s --limit-burst 3 -j ACCEPT
    iptables -A INPUT -p tcp --syn -j DROP
    iptables -A INPUT -p icmp -m limit --limit 1/s --limit-burst 1 -j ACCEPT
    iptables -A INPUT -p icmp -j DROP
    iptables -A INPUT -p udp -m limit --limit 10/s --limit-burst 20 -j ACCEPT
    iptables -A INPUT -p udp -j DROP
    iptables -A INPUT -p tcp --dport 80 -m connlimit --connlimit-above 10 -j REJECT --reject-with tcp-reset
    iptables -A INPUT -p tcp --dport 443 -m connlimit --connlimit-above 10 -j REJECT --reject-with tcp-reset
    iptables -A INPUT -p tcp --syn -m connlimit --connlimit-above 1000 -j DROP
    iptables -A INPUT -m recent --name portscan --rcheck --seconds 60 -j DROP
    iptables -A INPUT -m recent --name portscan --set -j ACCEPT
    iptables -A INPUT -p tcp --tcp-flags ALL NONE -j DROP
    iptables -A INPUT -p tcp --tcp-flags ALL ALL -j DROP
    iptables -A INPUT -m limit --limit 5/min -j LOG --log-prefix "IPTABLES_DROP: " --log-level 7
    if ! iptables -C INPUT -m set --match-set $BLACKLIST_NAME src -j DROP 2>/dev/null; then
        iptables -I INPUT -m set --match-set $BLACKLIST_NAME src -j DROP
    fi
    netfilter-persistent save 2>/dev/null || iptables-save > /etc/iptables/rules.v4
    log "iptables nastroen. Vse pravila aktivny."
}

setup_fail2ban() {
    log "Nastroyka Fail2Ban..."
    cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 10.0.0.0/8 192.168.0.0/16
bantime = 3600
findtime = 600
maxretry = 5
backend = systemd
banaction = iptables-multiport

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 3600

[nginx-http-auth]
enabled = true
port = http,https
filter = nginx-http-auth
logpath = /var/log/nginx/error.log
maxretry = 3
bantime = 3600

[nginx-req-limit]
enabled = true
port = http,https
filter = nginx-req-limit
logpath = /var/log/nginx/error.log
maxretry = 3
findtime = 60
bantime = 3600
action = iptables-multiport[name=HTTP, port=http,https, protocol=tcp]
EOF

    cat > /etc/fail2ban/filter.d/nginx-req-limit.conf << 'EOF'
[Definition]
failregex = ^.* limiting requests, excess: .* client: <HOST>.*$
ignoreregex =
EOF

    cat > /etc/fail2ban/filter.d/nginx-botsearch.conf << 'EOF'
[Definition]
failregex = ^<HOST> .* "(GET|POST|HEAD).*" 404 .*$
            ^<HOST> .* "(GET|POST|HEAD).*" 403 .*$
ignoreregex =
EOF

    systemctl restart fail2ban
    systemctl enable fail2ban
    log "Fail2Ban nastroen i zapushchen"
}

create_startup_script() {
    log "Sozdanie skripta avtozagruzki..."
    cat > /etc/init.d/ddos-shield << 'EOF'
#!/bin/bash
# /etc/init.d/ddos-shield
### BEGIN INIT INFO
# Provides:          ddos-shield
# Required-Start:    $network $remote_fs
# Required-Stop:     $network $remote_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: DDoS Shield Protection
# Description:       Avtomaticheskaya zashchita ot DDoS-atak
### END INIT INFO

case "$1" in
    start)
        echo "Zapusk DDoS Shield..."
        /usr/local/bin/ddos-shield --apply
        ;;
    stop)
        echo "Ostanovka DDoS Shield..."
        /usr/local/bin/ddos-shield --remove
        ;;
    restart)
        $0 stop
        $0 start
        ;;
    *)
        echo "Ispolzovanie: $0 {start|stop|restart}"
        exit 1
esac

exit 0
EOF

    chmod +x /etc/init.d/ddos-shield
    update-rc.d ddos-shield defaults
    log "Skript avtozagruzki sozdan"
}

install_main_program() {
    log "Ustanovka glavnoy programmy DDoS Shield..."
    cat > /usr/local/bin/ddos-shield << 'EOF'
#!/bin/bash

CONFIG_DIR="/etc/ddos-shield"
BLACKLIST_NAME="ddos_blacklist"
LOG_FILE="/var/log/ddos-shield.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a $LOG_FILE
}

show_status() {
    echo "============================================="
    echo "        STATUS ZASHCHITY DDoS              "
    echo "============================================="
    echo ""
    echo "PRAVILA IPTABLES:"
    iptables -L INPUT -n -v --line-numbers | head -20
    echo ""
    echo "CHERNY SPISOK IPSET:"
    ipset list $BLACKLIST_NAME 2>/dev/null || echo "Nabor $BLACKLIST_NAME ne sozdan"
    echo ""
    echo "STATUS FAIL2BAN:"
    systemctl status fail2ban --no-pager | head -5
    echo ""
    echo "STATISTIKA BLOKIROVOK:"
    if [[ -f /var/log/fail2ban.log ]]; then
        grep -c " Ban " /var/log/fail2ban.log 2>/dev/null || echo "Blokirovok poka net"
    fi
}

apply_rules() {
    log "Primenenie pravil zashchity..."
    if [[ -f /etc/ipset.conf ]]; then
        ipset restore < /etc/ipset.conf 2>/dev/null || ipset create $BLACKLIST_NAME hash:ip timeout 3600
    else
        ipset create $BLACKLIST_NAME hash:ip timeout 3600
    fi
    if [[ -f /etc/iptables/rules.v4 ]]; then
        iptables-restore < /etc/iptables/rules.v4
    fi
    systemctl start fail2ban
    log "Zashchita aktivirovana"
    show_status
}

remove_rules() {
    log "Udalenie pravil zashchity..."
    iptables -F INPUT
    iptables -F FORWARD
    iptables -F OUTPUT
    iptables -P INPUT ACCEPT
    iptables -P FORWARD ACCEPT
    iptables -P OUTPUT ACCEPT
    ipset flush $BLACKLIST_NAME 2>/dev/null
    systemctl stop fail2ban
    log "Zashchita deaktivirovana"
}

show_menu() {
    clear
    echo "============================================="
    echo "    DDoS Shield v1.0 - Sistema zashchity    "
    echo "============================================="
    echo ""
    echo "  1) Primenit pravila zashchity"
    echo "  2) Udalit pravila zashchity"
    echo "  3) Pokazat status zashchity"
    echo "  4) Pokazat logi"
    echo "  5) Zablokirovat IP vruchnuyu"
    echo "  6) Razblokirovat IP vruchnuyu"
    echo "  7) Vykhod"
    echo ""
    echo -n "Vyberite deystvie [1-7]: "
    read choice

    case $choice in
        1) apply_rules ;;
        2) remove_rules ;;
        3) show_status ;;
        4) tail -f $LOG_FILE ;;
        5) 
            echo -n "Vvedite IP dlya blokirovki: "
            read ip
            ipset add $BLACKLIST_NAME $ip
            echo "IP $ip zablokirovan"
            ;;
        6)
            echo -n "Vvedite IP dlya razblokirovki: "
            read ip
            ipset del $BLACKLIST_NAME $ip 2>/dev/null || echo "IP ne nayden"
            echo "IP $ip razblokirovan"
            ;;
        7) exit 0 ;;
        *) echo "Nevernyy vybor" ;;
    esac
}

case "$1" in
    --apply) apply_rules ;;
    --remove) remove_rules ;;
    --status) show_status ;;
    --menu) show_menu ;;
    *) show_menu ;;
esac
EOF

    chmod +x /usr/local/bin/ddos-shield
    log "Glavnaya programma ustanovlena"
}

finish_setup() {
    log "Zavershenie nastroyki..."
    chmod 755 /usr/local/bin/ddos-shield
    ln -sf /usr/local/bin/ddos-shield /usr/bin/ddos-shield
    /usr/local/bin/ddos-shield --apply
    echo ""
    echo -e "${GREEN}=============================================${NC}"
    echo -e "${GREEN}  USTANOVKA DDoS SHIELD ZAVERSHENA!     ${NC}"
    echo -e "${GREEN}=============================================${NC}"
    echo ""
    echo "Dlya upravleniya ispolzuyte komandu:"
    echo "   sudo ddos-shield"
    echo ""
    echo "Bystrye komandy:"
    echo "   sudo ddos-shield --apply    - primenit zashchitu"
    echo "   sudo ddos-shield --remove   - otklyuchit zashchitu"
    echo "   sudo ddos-shield --status   - pokazat status"
    echo ""
    echo "Dlya dobavleniya IP vruchnuyu:"
    echo "   sudo ipset add $BLACKLIST_NAME <IP-adres>"
    echo ""
}

main() {
    print_header
    check_root
    echo "Nachinaetsya ustanovka DDoS Shield..."
    echo ""
    install_dependencies
    setup_directories
    setup_ipset
    setup_iptables
    setup_fail2ban
    create_startup_script
    install_main_program
    finish_setup
}

main