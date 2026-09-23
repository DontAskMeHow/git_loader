# git_loader

Скрипты синхронизации локальной коллекции Git-репозиториев: обновление
клонов Bitbucket Server, добирание репозиториев, которых нет локально, и
подключение удалённого источника GitLab к существующим клонам (дополнительным
remote без переключения рабочего дерева).

## Состав

| Скрипт | Назначение |
| --- | --- |
| `update_bitbucket.py` / `update-bitbucket.ps1` | читает проекты Bitbucket Server через REST API, обновляет существующие клоны (`git fetch --all --prune --tags`), клонирует отсутствующие; пишет текстовый лог и CSV-отчёт в `logs/` |
| `import_gitlab_missing.py` | находит репозитории GitLab, которых нет среди локальных клонов, и клонирует их |
| `fetch_gitlab_remotes.py` | добавляет/обновляет remote `gitlab` на существующих клонах и подтягивает ветки (Bitbucket остаётся `origin`) |

## Требования

- Python 3.10+ (сторонних пакетов нет) либо PowerShell для `.ps1`-версии;
- Git в `PATH`;
- сетевой доступ к вашему GitLab/Bitbucket Server;
- учётные данные с правами на нужные проекты.

## Параметры

Все скрипты параметризуются аргументами; пароли не задаются в конфигах —
передавайте их через переменные окружения:

```text
--base-url     адрес сервера (например, https://bitbucket.example.com)
--destination  локальный каталог коллекции (например, D:\Repos\projects)
--username     логин
--dry-run      проба без изменений
```

## Пример

```powershell
$env:BITBUCKET_PASSWORD = 'your_password'
python .\update_bitbucket.py --base-url https://bitbucket.example.com --destination D:\Repos\projects --username user
python .\update_bitbucket.py --dry-run
```

Аналогично: `GITLAB_PASSWORD` для `import_gitlab_missing.py` и
`fetch_gitlab_remotes.py`.

Логи и отчёты попадают в `logs/` (каталог в `.gitignore`).

## Лицензия

MIT — см. [LICENSE](LICENSE).
