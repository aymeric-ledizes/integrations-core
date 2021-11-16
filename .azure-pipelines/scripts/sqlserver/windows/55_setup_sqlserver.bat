:: Set password
sqlcmd -Q "ALTER LOGIN sa with PASSWORD = 'Password12!';ALTER LOGIN sa ENABLE;"
sqlcmd -Q "CREATE LOGIN datadog WITH PASSWORD = 'Password12!';CREATE USER datadog FOR LOGIN datadog;"
sqlcmd -Q "GRANT SELECT on sys.dm_os_performance_counters to datadog;"
sqlcmd -Q "GRANT VIEW SERVER STATE to datadog;"
sqlcmd -Q "GRANT CONNECT ANY DATABASE to datadog;"
sqlcmd -Q "GRANT VIEW ANY DEFINITION to datadog;"
sqlcmd -Q "CREATE LOGIN bob WITH PASSWORD = 'hey-there-bob123';"
sqlcmd -Q "CREATE USER bob FOR LOGIN bob;"
sqlcmd -Q "GRANT CONNECT ANY DATABASE to bob;"

:: Enable port
powershell -Command "stop-service MSSQLSERVER"
powershell -Command "set-itemproperty -path 'HKLM:\software\microsoft\microsoft sql server\mssql14.MSSQLSERVER\mssqlserver\supersocketnetlib\tcp\ipall' -name tcpdynamicports -value ''"
powershell -Command "set-itemproperty -path 'HKLM:\software\microsoft\microsoft sql server\mssql14.MSSQLSERVER\mssqlserver\supersocketnetlib\tcp\ipall' -name tcpport -value 1433"
powershell -Command "set-itemproperty -path 'HKLM:\software\microsoft\microsoft sql server\mssql14.MSSQLSERVER\mssqlserver\' -name LoginMode -value 2"
powershell -Command "start-service MSSQLSERVER"
