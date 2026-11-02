# Security-обновление ядра Ubuntu — 2 ноября 2026

После обновления индексов APT обнаружены три обновляемых метапакета ядра с origin noble-security: linux-generic-hwe-24.04, linux-image-generic-hwe-24.04 и linux-headers-generic-hwe-24.04. Кандидат7.0.0-42.42~24.04.1. Другие накопившиеся обновления не объявляются security-патчами лишь на основании наличия новых версий.

Независимо проверенный sourcee40500d установлен на тестовомadmins-MS-7D22 и выполнен через полныйMCP: operation08a408604bd946d48aa9db0c0b612ff3 success. Установлены новые ядро, модули, заголовки и tools; apt/initramfs/GRUBзавершились безошибки. Старые ядра31/38 не удалялись. Скрипт не выполняет autoremove или reboot. РаботающиеSSH/1Panel/MCP/Docker после установки active; новый SSHпо ключу+sudo работает.

**Пока работает ядро7.0.0-38. Новое42 только установлено; защита нового ядра ещё не активна.** Перед перезагрузкой уточняется доступность консоли для выбора старого ядра при неудачной загрузке. После reboot обязательны uname-r, новыйSSHkey+sudo, rootMCP,1PanelHTTPS,ASAready/containers/FRP и проверка данных. Установка пакета не является доказательством успешной загрузки.

```bash
# План на Ubuntu с python3-apt; индексы предварительно обновить
sudo apt-get update
sudo python3 scripts/update-security-kernel.py
# Только после рассмотрения плана и независимого review source
sudo python3 scripts/update-security-kernel.py --apply --expected-host ACTUAL_HOSTNAME
```

Скрипт проверяет security-origin метапакетов, фиксирует их версии и показывает simulatedtransaction. Отклоняет removals/Instнесвязанныхпакетов. Реальныйapt-getтакже использует --no-remove; NEEDRESTART_MODE=l предотвращает автоматические рестарты сервисов со стороны needrestart. При этом стандартные maintainerhooks пакетов ядра выполняются; это не универсальный запрет любых сервисных действий. Проверяется сохранение образа работающего ядра. Транзитивные зависимости полностью не pinned, поэтому не заявляется byte-exact воспроизведение симуляции. Новые неподписанные/private репозитории не добавлялись.

Проверки: Pythoncompile/diffcheck;3focusedtests (plan/no-remove, securityorigin, rejectremoval/nonkernel), liveAPTsimulation, реальная установка и postchecks PASS. Полная программа безопасности остаётся открытой (docs31); productdeploymentblocker ELECTRONICS-836 из текущегоASAконтракта не отменялся.

## Проверка входа1Panel

Владелец подтвердил успешный вход после исправления publickey-cookie. Это дополнительно подтверждено серверным loginlog: Success2026-11-02T21:50:42.979239993+03:00. Пароль/капча не извлекались, аккаунт не сбрасывался. Ранее заявленная только доступность формы теперь дополнена реальным успешным входом владельца.
