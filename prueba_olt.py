from netmiko import ConnectHandler

olt = {
    "device_type": "huawei_smartax",
    "host": "192.168.3.250",
    "port": 2222,
    "username": "root",
    "password": "admin123",
    "session_log": "olt_log.txt",   # guarda todo lo que pasa en la sesión
}

with ConnectHandler(**olt) as con:
    con.enable()        # MA5800-X7>  ->  MA5800-X7#
    con.config_mode()   # MA5800-X7#  ->  MA5800-X7(config)#
    print(con.send_command("display ont autofind all"))
    print(con.send_command("display ont info 0 1 0 0"))