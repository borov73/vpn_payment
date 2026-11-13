# FirstByte VPS: Ubuntu, Termius и systemd

Инструкция рассчитана на **Ubuntu 24.04 LTS с Python 3.12** и SSH-пользователя с sudo.
Эта процедура проверена на FirstByte 13.11.2026; результаты — в
[отчёте](../docs/VERIFICATION.md). В Termius на Mac добавьте Host с IP,
SSH-портом, пользователем и вашим SSH-ключом/паролем из панели FirstByte. Эти данные
не относятся к кошельку и не должны попадать в репозиторий.

Бот использует исходящие HTTPS-соединения к Telegram, RPC и биржам. Входящий порт
для бота/домен не нужен. Все команды ниже выполняются на VPS через Termius, если
не указано иное. Используйте одного владельца Telegram-бота и только один экземпляр
polling: остановите локальный `run` перед запуском сервиса на VPS.

Публичный GitHub содержит только исходники. Доступ к VPS остаётся по SSH с вашим
ключом/паролем; `.env`, база и копии не публикуются. Не открывайте для бота входящие
HTTP-порты и не добавляйте SSH-ключи посторонних. Telegram доступен только вашему User ID.

## 1. Установка проекта

```sh
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git ca-certificates
sudo useradd --system --home /opt/vpn_payment --shell /usr/sbin/nologin vpnpayment
sudo install -d -o "$USER" -g vpnpayment -m 2755 /opt/vpn_payment
sudo install -d -o vpnpayment -g vpnpayment -m 700 /var/lib/vpn-payment
sudo install -d -o vpnpayment -g vpnpayment -m 700 /var/backups/vpn-payment
```

Создание пользователя делается один раз; если он уже существует, не повторяйте
`useradd`. Публичный репозиторий можно клонировать без авторизации:

```sh
cd /opt/vpn_payment
git clone https://github.com/borov73/vpn_payment.git .
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
sudo chown "$USER":vpnpayment .env
chmod 640 .env
nano .env
```

При использовании своего fork замените URL адресом своего репозитория.
Боту не нужен GitHub-токен.

## 2. Конфигурация и первая синхронизация

В `.env` обязательно укажите:

```dotenv
TELEGRAM_BOT_TOKEN=настоящий_токен_бота
TELEGRAM_USER_ID=ваш_числовой_User_ID
BASE_RPC_URL=https://ваш_надёжный_archive_RPC_Base
DATABASE_PATH=/var/lib/vpn-payment/vpn_payment.sqlite3
```

`DATABASE_PATH` на VPS должен быть **абсолютным и внутри `/var/lib/vpn-payment`**:
systemd намеренно разрешает запись только туда. Также заполните в приватном `.env`
`SENDER_ADDRESS`, `RECIPIENT_ADDRESSES`, `ACTIVE_RECIPIENT_ADDRESSES` и `HISTORY_START`
по таблице в [README](../README.md#-настройка). Дата поиска должна быть не позднее
первого учитываемого платежа. Персональных адресов в образце нет.
`SUBSCRIPTION_START` при создании базы задаёт подтверждённое расчётное начало;
при переносе существующей базы сохраняются её правила. Старых получателей оставляйте
в общем списке, а в активном указывайте только действующие адреса.

Откройте своего бота, нажмите **Start**, затем:

```sh
cd /opt/vpn_payment
sudo -u vpnpayment .venv/bin/python -m vpn_payment doctor
sudo -u vpnpayment .venv/bin/python -m vpn_payment doctor --online
sudo -u vpnpayment .venv/bin/python -m vpn_payment sync
sudo -u vpnpayment .venv/bin/python -m vpn_payment status
```

Повторяйте `sync` до окончания диапазона или доверьте продолжение сервису. Если
archive RPC недоступен, используйте ручные хеши и режимы из README. Для ручного
импорта остановите сервис, выполните CLI-импорт и запустите сервис снова — CLI
sync при работающем боте блокируется межпроцессным lock. `status` и согласованный `backup` разрешены параллельно сервису.

Можно перенести уже проверенную локальную историю: на Mac выполните
`python -m vpn_payment backup`, передайте полученный `.sqlite3` через Termius SFTP
в приватный временный файл на VPS, затем **до первого запуска сервиса** установите
его как `/var/lib/vpn-payment/vpn_payment.sqlite3` с владельцем `vpnpayment:vpnpayment`
и правами `600`. Переносите backup, а не живой DB-файл. Адрес отправителя и
`HISTORY_START` должны совпадать с исходной базой; Telegram-настройки в `.env` переносите отдельно.

## 3. Сервис и автозапуск

```sh
sudo install -m 644 deploy/vpn-payment.service /etc/systemd/system/vpn-payment.service
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-payment
sudo systemctl status vpn-payment --no-pager
sudo journalctl -u vpn-payment -n 100 --no-pager
```

`Restart=on-failure` перезапускает процесс после сбоя; `enable` включает запуск после
перезагрузки. При ручном `stop` сервис не перезапускается. Временная недоступность
RPC/CCXT обрабатывается внутри приложения с сохранением состояния. Если процесс
быстро завершился много раз, исправьте `.env`, затем `systemctl reset-failed vpn-payment`
и `systemctl start vpn-payment`.

```sh
sudo journalctl -u vpn-payment -f
sudo systemctl stop vpn-payment
sudo systemctl start vpn-payment
sudo systemctl restart vpn-payment
```

Команды представлены отдельно: `stop` — безопасная остановка, `start`/`restart` —
явный запуск. Для отключения автозапуска: `sudo systemctl disable --now vpn-payment`.
После успешного `enable --now` Termius можно закрыть, а Mac выключить: бот работает
на VPS. Не запускайте одновременно локальную копию с тем же Telegram-токеном.

## 4. Ежедневный backup

```sh
sudo install -m 644 deploy/vpn-payment-backup.service /etc/systemd/system/
sudo install -m 644 deploy/vpn-payment-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vpn-payment-backup.timer
sudo systemctl start vpn-payment-backup.service
sudo systemctl list-timers vpn-payment-backup.timer
sudo journalctl -u vpn-payment-backup.service -n 30 --no-pager
```

Таймер запускается ежедневно около **03:00 UTC / 06:00 Europe/Moscow** с разбросом
до пяти минут и наверстывает пропущенный запуск после перезагрузки. Копии лежат
в `/var/backups/vpn-payment`, права `600`. Скрипт `deploy/backup.sh` использует SQLite
backup API и `integrity_check`; живую базу останавливать не нужно.

После успешного создания и проверки новой копии автоматически удаляются ежедневные
копии старше **5 суток**. Очистка затрагивает только файлы формата
`vpn-payment-YYYYMMDDTHHMMSS-N.sqlite3` непосредственно в каталоге копий;
ручные копии перед обновлением/восстановлением, подкаталоги и ссылки сохраняются.
Вместе с истёкшей копией удаляются её обычные файлы SQLite `-wal`/`-shm`, если они остались.
Если новый backup не удался, старые копии не удаляются. Рабочая база хранит всю
историю независимо от срока хранения backup. Периодически переносите нужные копии
за пределы VPS через SFTP и проверяйте восстановление.
База содержит личную историю; не выкладывайте её в GitHub. `.env` резервируется
отдельно с приватными правами, не через Git.

## 5. Обновление через Git

### Если проект уже установлен без Git

Этот шаг нужен один раз **после публикации**. `git clone ... .` не подходит для
непустого `/opt/vpn_payment`. Следующие команды подключают существующие файлы
к опубликованной ветке, сохраняя рабочие файлы, приватный `.env` и внешнюю базу:

```sh
cd /opt/vpn_payment
sudo -u vpnpayment .venv/bin/python -m vpn_payment backup --output "/var/backups/vpn-payment/before-git-$(date -u +%Y%m%dT%H%M%S).sqlite3"
sudo systemctl stop vpn-payment
git init -b main
git remote add origin https://github.com/borov73/vpn_payment.git
git fetch origin main
git reset --mixed origin/main
git branch --set-upstream-to=origin/main main
git diff --exit-code
git status --short
```

Выполняйте это от владельца каталога, указанного при установке. `reset --mixed`
устанавливает состояние Git без перезаписи рабочих файлов. `git diff` и `status`
должны быть пустыми: на VPS и в опубликованной версии ожидаются одинаковые исходники.
Если есть различия, сначала разберите их; не используйте `reset --hard` или `clean`
для удаления файлов. Если `.git` или `origin` уже есть, повторная инициализация не нужна.
После успешной проверки:

```sh
sudo -u vpnpayment .venv/bin/python -m vpn_payment doctor
sudo systemctl start vpn-payment
sudo systemctl status vpn-payment --no-pager
```

### Последующие обновления

Сначала тестируйте изменение локально и отправляйте в свой репозиторий.
На VPS из `/opt/vpn_payment`:

```sh
sudo -u vpnpayment .venv/bin/python -m vpn_payment backup --output "/var/backups/vpn-payment/before-update-$(date -u +%Y%m%dT%H%M%S).sqlite3"
sudo systemctl stop vpn-payment
git status --short
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
sudo -u vpnpayment .venv/bin/python -m vpn_payment doctor
sudo systemctl start vpn-payment
sudo systemctl status vpn-payment --no-pager
sudo journalctl -u vpn-payment -n 50 --no-pager
```

Если любой шаг не прошёл, выясните причину до запуска следующего. `.env` и база не
перезаписываются `git pull`. Не удаляйте их при обновлении. После изменения unit-файлов
заново установите только изменённые units, выполните `daemon-reload` и перезапуск.
Изменения `.env` тоже требуют перезапуска. Новая версия не должна понижать схему БД.

## 6. Восстановление базы

Остановите `vpn-payment` и на время восстановления `vpn-payment-backup.timer`.
Сделайте согласованную копию текущей базы и проверьте выбранный backup:

```sh
sudo systemctl stop vpn-payment vpn-payment-backup.timer vpn-payment-backup.service
sudo -u vpnpayment .venv/bin/python -m vpn_payment backup --output "/var/backups/vpn-payment/before-restore-$(date -u +%Y%m%dT%H%M%S).sqlite3"
```

Перед заменой файлов проверьте выбранную копию
(подставьте свой путь); команда открывает только существующий файл и проверяет
целостность и наличие таблицы платежей:

```sh
python3 - /ПУТЬ/К/ВЫБРАННОЙ_КОПИИ.sqlite3 <<'PY'
from pathlib import Path
import sqlite3
import sys

uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro&immutable=1"
with sqlite3.connect(uri, uri=True) as db:
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit("Backup integrity check failed: " + result)
    count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print("Backup OK; payments:", count)
PY
```

При любой ошибке не продолжайте замену. `doctor` проверяет конфигурацию, а не
целостность выбранной копии. После проверки переместите текущие `.sqlite3`,
`.sqlite3-wal`, `.sqlite3-shm`, если они существуют, в отдельную приватную папку
для отката. Не оставляйте старый WAL рядом с восстанавливаемой базой.
Затем установите backup:

```sh
sudo install -o vpnpayment -g vpnpayment -m 600 /ПУТЬ/К/ВЫБРАННОЙ_КОПИИ.sqlite3 /var/lib/vpn-payment/vpn_payment.sqlite3
sudo -u vpnpayment .venv/bin/python -m vpn_payment doctor
sudo systemctl start vpn-payment
sudo systemctl start vpn-payment-backup.timer
```

Не выполняйте команду с буквальным путём-примером и не восстанавливайте поверх
работающей базы. После восстановления бот продолжит с сохранённых курсоров;
журнал уведомлений из backup предотвращает повторы в пределах этой копии. Более
старая копия не содержит уведомлений, отправленных после неё, поэтому их повторы
при восстановлении старого backup теоретически возможны.
