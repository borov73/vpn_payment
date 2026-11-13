# Публикация проекта в GitHub

Репозиторий проекта: [borov73/vpn_payment](https://github.com/borov73/vpn_payment).
Ниже описаны первая публикация и последующие обновления. Создание репозитория
выполняется через GitHub CLI, отправка кода — по SSH, подпись коммитов — GPG.

## 1. Авторизация на Mac

```sh
brew install gh
gh auth login --hostname github.com --git-protocol ssh --web
gh auth status
ssh -T git@github.com
```

Откройте предложенную страницу браузера и подтвердите свой аккаунт. Не вставляйте
токен в исходники, командную строку или `.env` бота. GitHub-доступ независим от Telegram.
Официальная справка: [gh auth login](https://cli.github.com/manual/gh_auth_login).
Успешный `ssh -T` сообщает имя аккаунта и обычно возвращает код 1, потому что GitHub
не предоставляет shell. Для проверки используйте именно сообщение об авторизации.

## 2. Локальный репозиторий и первый коммит

Из корня проекта (если Git ещё не инициализирован):

```sh
git init -b main
git status --short
git check-ignore .env var/vpn_payment.sqlite3 .venv/ backups/
git add .
git diff --cached --stat
git diff --cached --name-only
git ls-files -- '.env' '*.db' '*.sqlite3' '*.pem' '*.key'
```

Последняя команда должна вывести **пустой результат**. `.env.example` с пустыми
секретами должен быть в коммите. Для поиска возможных токенов без вывода значений:

```sh
git grep --cached -l -E '[0-9]{8,}:[A-Za-z0-9_-]{30,}'
```

Здесь также ожидается отсутствие совпадений; exit code 1 у `git grep` означает,
что совпадений нет. Поиск — дополнительная проверка, не универсальный детектор секретов.
Просмотрите staged diff локально. Затем:

```sh
git commit -S -m "feat: add personal Base VPN payment tracker"
git verify-commit HEAD
```

Если Git просит имя/email, задайте свои настоящие значения через `git config user.name`
и `git config user.email` для этого репозитория. Codex не подставляет чужую подпись.

Для GPG-подписи настройте свой ключ и включите подпись в этом репозитории:

```sh
git config gpg.format openpgp
git config user.signingkey ВАШ_ОТПЕЧАТОК_GPG
git config commit.gpgsign true
```

Публичная часть GPG-ключа должна быть добавлена в GitHub → Settings → SSH and GPG keys;
email коммита должен совпадать с email ключа и быть подтверждён в аккаунте.
После push проверьте **Verified** у коммита. Не отключайте подпись ради обхода ошибки.
Приватный GPG-ключ и пароль остаются на Mac. Email в истории коммитов публичен.
Справка: [проверка подписей GitHub](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification).

## 3. Создание публичного репозитория и push

Эти команды создают удалённый репозиторий и отправляют код — выполняйте их, когда
решите публиковать. По пожеланию владельца выбран **публичный** репозиторий:

```sh
gh repo create vpn_payment --public --source=. --remote=origin
git remote -v
git push -u origin main
gh repo view --json nameWithOwner,visibility,url
```

Перед push проверьте SSH-адрес `git@github.com:borov73/vpn_payment.git`
(для своего fork укажите его владельца). Проверьте `visibility: PUBLIC`. Справка: [gh repo create](https://cli.github.com/manual/gh_repo_create).
Если `origin` уже существует, сначала проверьте `git remote -v`, а не заменяйте его вслепую.

## 4. Следующие изменения

```sh
python -m pytest -q
ruff check vpn_payment tests
git status --short
git diff
git add vpn_payment tests README.md deploy docs requirements.in requirements.txt requirements-dev.in requirements-dev.txt
git diff --cached --stat
git ls-files -- '.env' '*.db' '*.sqlite3' '*.pem' '*.key'
git commit -S -m "fix: describe the concrete change"
git verify-commit HEAD
git push
```

Стадируйте только действительно изменённые файлы; конфигурация CI и `.env.example`
добавляются отдельно при их изменении. Практичный порядок дальнейших коммитов:
1) конфигурация/БД, 2) blockchain и курсы, 3) billing с тестами, 4) bot и уведомления,
5) deploy/docs. Начальная готовая версия также может быть одним проверенным коммитом.

Проверьте историю имён файлов:

```sh
git log --all --format= --name-only -- '.env' '*.db' '*.sqlite3' '*.pem' '*.key'
```

`.gitignore` не удаляет файлы из прошлых коммитов. Если настоящий токен когда-либо
попал в Git, немедленно отзовите/замените его у выдавшего сервиса, затем отдельно
очистите историю. Не считайте удаление последней копии токена достаточной защитой.

## 5. Обновления на VPS

Публичный репозиторий клонируется и обновляется без авторизации на GitHub.
Боту на VPS не нужен GitHub-токен. Отправлять изменения в репозиторий может
владелец со своего Mac после авторизации.

Публичная публикация исходников не открывает доступ к серверу: SSH требует вашей
авторизации. Не добавляйте в репозиторий `.env`, базу, SSH-ключи, IP/имя своего VPS
или токены; для работы бота не нужны GitHub Actions с доступом к серверу.

Если бот уже установлен на VPS без `.git`, сначала выполните одноразовое подключение
из раздела «Если проект уже установлен без Git» в [инструкции VPS](../deploy/README.md).
Клонирование в его непустую папку не требуется. Там же описаны новая установка и обновления.
Кратко: согласованный backup → `systemctl stop` → `git pull --ff-only` → установка
зафиксированных зависимостей → `doctor` → `systemctl start` и проверка журнала.
Бот работает от отдельного системного пользователя и не нуждается в GitHub-токене.
