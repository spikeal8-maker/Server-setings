# Проверка фактических образов2ноября2026

На тестовомadmins-MS-7D22 установлен официальныйTrivy0.76.1. Архивtrivy_0.76.1_Linux-64bit.tar.gz загружен сGitHubaquasecurity/trivy; SHA256c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0 совпал софициальнымchecksumsфайлом. Изtar извлечён только обычныйфайлtrivy, никакиеinstall-shellскрипты не исполнялись. Приватное размещение /opt/server-settings/security-tools/trivy-0.76.1; DockerнаD2неиспользовался.

MCPoperation782300191edc41a89f0365558239cfff success. На домашнемUbuntu выполнен read-only scan точныхrunning imageIDs через existingDockerAPI: --scannersvuln --severityHIGH,CRITICAL --ignore-unfixed. ПолныеJSON/логи сохраняются root-only под /var/log/asa-security и не публикуются; rawenv/секреты не запрашивались. Каждая команда scannerexit0 означает завершение сканирования, не отсутствиеуязвимостей.

|Образ|ИсправляемыеHigh/Critical findings|
|---|---:|
|ASAapi|23|
|ASAweb|101|
|PostgreSQL17.7-bookworm|86|
|1PanelOpenResty|0, ноEOLwarning—неприняткакчистый|

Это количество findings по пакетам, не обязательно уникальныеCVE; не включаетunfixed/medium/low. ДляOpenResty сканерпредупредил, чтоОСбаза больше не получает обычныеsecurityupdates и обнаружение можетбытьнеполным. Поэтому ноль не закрываетриск. Нужны поддерживаемые patchedобразы, сборкаи повторныйscan, а не правка runningcontainerвручную.

Примерыpatchtargets: Debianlibcap2 1:2.66-4+deb12u3,libgnutls30 3.7.9-2+deb12u7; Alpinec-ares1.34.8-r0,curl8.14.1-r4,libcrypto3 3.5.6-r0. ОбновленияОСхоста не обновляютэтипакетывнутриконтейнеров. Отдельныйsourceсрезsecurity/image-baselines-20261102назначенисполнителю; runtimeисправлениепоканеобъявляетсяпринятым.

Повторение: взятьофициальныйпроверенныйbinaryтойжеверсии, определитьimageID черезdockerinspect, выполнитьtrivyimageс указаннымиfilters иroot-onlyoutput. СохранятьдатуDB иactualimageID. Документацияvendor: https://trivy.dev/docs/latest/getting-started/installation/ .
