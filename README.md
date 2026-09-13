# Vector-RU: русский голос, локальная LLM и умный дом для Anki Vector

Anki Vector (2018) учится говорить по-русски, думать локальной LLM и управлять
умным домом через Home Assistant. Облако Anki не используется вообще: робот
работает через [wire-pod](https://github.com/kercre123/wire-pod), а этот демон
добавляет то, чего не умеет ни прошивка, ни wire-pod — русскую озвучку.

**Что получается в итоге:**

- «Hey Vector, вопрос — почему небо синее?» → ответ локальной LLM **по-русски**,
  голосом «под Вектора» (питч-шифт, как делали в Anki), с анимацией-эмоцией;
- «Hey Vector, включи свет на кухне» → Home Assistant включает свет, робот
  подтверждает по-русски;
- «Hey Vector, вопрос, поставь таймер на пять минут» → локальный таймер,
  по истечении робот сам объявляет «Динь-динь!»;
- «тише» / «громче» — управление громкостью озвучки.

## Архитектура

```
"Hey Vector, <фраза>"
        │
   wire-pod (STT Vosk ru-RU, кастомный интент — триггер-слова)
        │  exec: trigger.sh "<фраза>" <serial>   (мгновенно уходит в фон)
        ▼
vector_ru_daemon.py (Flask, systemd-сервис)
        ├── локальные команды (таймеры, громкость) — без LLM
        ├── LLM_BACKEND=openai        → Ollama/LM Studio (OpenAI-совместимый API)
        │   LLM_BACKEND=homeassistant → HA Conversation API (LLM + умный дом)
        ├── тег эмоции [радость]/[грусть]/... → анимация робота
        ├── TTS: Silero v4 (CPU, быстрее реального времени) или XTTS v2
        ├── ffmpeg: питч-шифт "под Вектора" → WAV 16000 Гц / mono / 16-bit
        └── SDK (wirepod-vector-sdk) → динамик робота
```

## Требования

- Anki Vector, переведённый на wire-pod (любая установка: deb, docker, исходники);
- Linux-хост/LXC для демона: 2+ ядра, 4 ГБ RAM (8 ГБ для XTTS), Python 3.10–3.12;
- локальная LLM: Ollama / LM Studio / llama.cpp с OpenAI-совместимым API;
- опционально: Home Assistant с интеграцией Ollama (для умного дома);
- опционально: GPU (см. раздел про LXC + NVIDIA).

## Файлы репозитория

| Файл | Куда кладётся | Что делает |
|---|---|---|
| `vector_ru_daemon.py` | `/opt/vector-ru/` (хост демона) | основной сервис |
| `vector-ru.service` | `/etc/systemd/system/` (хост демона) | systemd-юнит, вся конфигурация |
| `trigger.sh` | `/opt/vector-ru/` (хост **wire-pod**) | мост wire-pod → демон |

## Установка

### 1. Wire-pod: русский STT и LLM (если ещё не сделано)

В веб-интерфейсе wire-pod (`:8080`) переключите язык на русский (Vosk-модель
подтянется сама). Wake word остаётся английским «Hey Vector» — он зашит в
прошивку и не меняется.

### 2. Демон: зависимости

```bash
apt install -y python3-venv python3-pip ffmpeg curl
mkdir -p /opt/vector-ru && cd /opt/vector-ru
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
./venv/bin/pip install wirepod-vector-sdk flask requests num2words numpy omegaconf
# опционально, для XTTS v2 (медленно на CPU, нужен GPU или терпение):
# ./venv/bin/pip install coqui-tts
```

Проверка SDK: `./venv/bin/python -c "import anki_vector; print('SDK OK')"`.
Если на Python 3.12 посыпались ошибки protobuf/grpc — поставьте 3.11
(deadsnakes PPA) и пересоздайте venv.

### 3. Сертификаты SDK

SDK ищет `~/.anki_vector/sdk_config.ini` + сертификат робота у пользователя,
от которого запущен демон.

**Штатный путь.** Wire-pod создаёт эти файлы при авторизации робота. Точное
место он печатает в лог при старте:

```bash
journalctl -u wire-pod | grep "SDK info path"
```

Скопируйте найденную папку в `/root/.anki_vector` на хост демона и проверьте
в `sdk_config.ini` поля `ip` (текущий IP робота, без порта) и `cert`
(абсолютный путь к .cert-файлу на новом месте).

**Известный баг deb-установки:** wire-pod может писать SDK-файлы в
`/tmp/.anki_vector/`, который очищается при перезагрузке. Лечение — постоянная
символическая ссылка (на хосте wire-pod):

```bash
mkdir -p /root/.anki_vector
rm -rf /tmp/.anki_vector
cat > /etc/tmpfiles.d/wirepod-sdk.conf <<'EOF'
d /root/.anki_vector 0755 root root -
L /tmp/.anki_vector - - - - /root/.anki_vector
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/wirepod-sdk.conf
```

**Если сертификат утерян** — его можно снять прямо с робота (guid берётся из
`.../wire-pod/jdocs/botSdkInfo.json`, в deb-версии — `/etc/wire-pod/wire-pod/...`):

```bash
openssl s_client -connect IP_РОБОТА:443 -showcerts </dev/null 2>/dev/null \
  | openssl x509 -outform PEM > /root/.anki_vector/robot.cert
openssl x509 -in /root/.anki_vector/robot.cert -noout -subject   # CN = Vector-XXXX
```

`sdk_config.ini`:

```ini
[серийник]
cert = /root/.anki_vector/Vector-XXXX-серийник.cert
ip = 192.168.x.x
name = Vector-XXXX
guid = значение_из_botSdkInfo.json
```

Проверка (робот должен сказать «test»):

```bash
/opt/vector-ru/venv/bin/python -c "
import anki_vector
with anki_vector.Robot() as r: r.behavior.say_text('test')"
```

### 3.1 Proxmox: общий bind-mount вместо копирования (рекомендуется)

Если wire-pod и демон живут в **разных LXC-контейнерах одного Proxmox-хоста**
(типичная схема: `escapepod` — wire-pod, `Silero` — этот демон), ручное
копирование `~/.anki_vector` бьётся о баг из раздела выше: wire-pod пишет
SDK-файлы в `/tmp/.anki_vector/`, который стирается при каждой перезагрузке
контейнера. После стирания wire-pod переавторизуется у робота и получает
**новый guid** — а скопированная на хост демона копия остаётся со старым и
ловит `StatusCode.UNAUTHENTICATED: 401 Failed to authenticate request`.
Особенно неприятно это после отключения электричества, когда перезагружаются
сразу оба контейнера.

Решение — не копировать файл, а сделать `/root/.anki_vector` **одной и той же
директорией на хосте**, примонтированной в оба контейнера. Тогда wire-pod и
демон всегда видят один и тот же актуальный guid, без всякой синхронизации.

**На хосте Proxmox** (замените `101`/`112` на свои CTID):

```bash
mkdir -p /srv/vector-sdk
# перенести туда текущие рабочие файлы (см. раздел 3, откуда их взять)
chown -R 100000:100000 /srv/vector-sdk   # если контейнеры unprivileged (root=uid 100000 на хосте)
# chown -R root:root /srv/vector-sdk     # если privileged

pct stop 101
pct stop 112
pct set 101 -mp0 /srv/vector-sdk,mp=/root/.anki_vector
pct set 112 -mp0 /srv/vector-sdk,mp=/root/.anki_vector
pct start 101
pct start 112
```

**Внутри контейнера wire-pod** — wire-pod всё равно хардкодит путь
`/tmp/.anki_vector`, поэтому подменяем его постоянной символической ссылкой
на общий mount (systemd-tmpfiles пересоздаёт её при каждой загрузке, так что
контейнер можно перезагружать сколько угодно):

```bash
mkdir -p /root/.anki_vector
rm -rf /tmp/.anki_vector
cat > /etc/tmpfiles.d/wirepod-sdk.conf <<'EOF'
d /root/.anki_vector 0755 root root -
L /tmp/.anki_vector - - - - /root/.anki_vector
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/wirepod-sdk.conf
systemctl restart wire-pod
```

Проверка, что оба контейнера видят один и тот же файл:

```bash
pct exec 101 -- cat /root/.anki_vector/sdk_config.ini
pct exec 112 -- cat /root/.anki_vector/sdk_config.ini   # должно совпасть
```

**Если guid всё же разошёлся к моменту, когда вы это настраиваете** (типичный
случай — вы уже пытались чинить проблему копированием и в общий файл попала
устаревшая версия) — источник истины не `/tmp/.anki_vector`, а собственная
база wire-pod:

```bash
cat /etc/wire-pod/wire-pod/jdocs/botSdkInfo.json   # deb-версия; из исходников — wire-pod/jdocs/...
```

Возьмите `guid` оттуда для нужного `esn` и пропишите его в общий
`sdk_config.ini` вручную (заодно можно перевыпустить сертификат — команда в
начале раздела 3).

### 4. Демон: установка и настройка

```bash
cp vector_ru_daemon.py /opt/vector-ru/
cp vector-ru.service /etc/systemd/system/
```

Отредактируйте настройки в `vector-ru.service` (см. справочник ниже), затем:

```bash
systemctl daemon-reload && systemctl enable --now vector-ru
journalctl -u vector-ru -f        # ждём "Демон запущен на порту 8090"
curl http://127.0.0.1:8090/health
```

Тест всей цепочки без wake word:

```bash
curl -X POST http://127.0.0.1:8090/ask \
  --data-urlencode "text=вопрос расскажи что-нибудь интересное" \
  --data-urlencode "serial=СЕРИЙНИК"
```

### 5. Триггер на хосте wire-pod

```bash
mkdir -p /opt/vector-ru
cp trigger.sh /opt/vector-ru/ && chmod +x /opt/vector-ru/trigger.sh
```

В `trigger.sh` укажите адрес демона (`DAEMON_URL`). Внимание: файл должен быть
с Unix-переводами строк — если скопировали через Windows, `exec format error`
лечится `sed -i 's/\r$//' trigger.sh`.

### 6. Кастомный интент в wire-pod

Веб-интерфейс `:8080` → Custom Intents → Add:

| Поле | Значение |
|---|---|
| Name | `russian_chat` |
| Utterances (через запятую) | `вопрос,спроси,поболтаем,слушай,включи,выключи,открой,закрой,поставь,запусти,останови,убавь,прибавь,таймер,тише,громче` |
| Intent to send | `intent_imperative_praise` |
| Program to launch | `/opt/vector-ru/trigger.sh` |
| Arguments | `!speechText,!botSerial` ← **через запятую!** |

Как это работает: wire-pod проверяет кастомные интенты раньше встроенных и
ищет utterance как подстроку в любом месте фразы. Слова из `TRIGGERS` в юните
(«вопрос», «спроси»...) демон вырезает из фразы; всё остальное («включи свет»)
уходит в LLM/HA целиком.

**Перехват вообще всех фраз:** задайте utterances `а,о,е,и,у,э,ы,я,ю` —
совпадёт с любой русской фразой. Встроенные функции робота при этом перестанут
срабатывать (таймеры и громкость демон компенсирует своими).

Если используете Knowledge Graph / Intent Graph в wire-pod — выключите их,
иначе фразы без триггера будут озвучиваться бортовым английским TTS.

## Home Assistant (умный дом)

1. HA → Настройки → Устройства и службы → **Ollama** → Параметры:
   включить **«Управление Home Assistant»**, модель с поддержкой инструментов,
   контекст 16384, историю сообщений 5–10.
2. Настройки → Голосовые ассистенты: открыть (expose) нужные устройства,
   задать им русские имена/псевдонимы и помещения. Включить
   **«Предпочитать локальную обработку команд»** — стандартные команды
   («включи свет на кухне») исполняются движком интентов HA мгновенно и
   без LLM; в LLM уходит только то, что не подошло под шаблоны.
3. Профиль → создать долгосрочный токен. Id агента — в Средствах разработчика
   → Действия → `conversation.process` (вида `conversation.ollama_xxx`).
4. В `vector-ru.service`: `LLM_BACKEND=homeassistant`, `HA_URL`, `HA_TOKEN`,
   `HA_AGENT_ID`.
5. Системный промпт в этом режиме задаётся в настройках интеграции Ollama
   (поле Instructions) — добавьте туда требования про русский, краткость и
   тег эмоции (см. ниже), причём с оговоркой, что тег ставится только в
   финальном текстовом ответе, ПОСЛЕ вызова инструментов.

Рекомендации по моделям (8 ГБ VRAM): `qwen3:8b` (лучший баланс
русский/инструменты), `qwen3:4b-instruct` (быстрее), `qwen2.5:7b`.
При «модель говорит, что включила, но не включила»: убедиться, что включено
«Управление HA»; снизить температуру (производная модель через `ollama create`
с `PARAMETER temperature 0.2`); сократить историю; не требовать тег эмоции
ПЕРЕД вызовом инструментов — это ломает tool calling у дисциплинированных
моделей (Qwen).

## Справочник настроек (`vector-ru.service`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_BACKEND` | `openai` | `openai` (Ollama напрямую) / `homeassistant` |
| `LLM_URL` | `http://127.0.0.1:11434/v1/chat/completions` | endpoint LLM |
| `LLM_MODEL` | `qwen2.5:7b` | модель для режима openai |
| `HA_URL`, `HA_TOKEN`, `HA_AGENT_ID`, `HA_LANGUAGE` | — | параметры HA |
| `TTS_ENGINE` | `silero` | `silero` / `xtts` |
| `SILERO_SPEAKER` | `xenia` | `aidar` `baya` `kseniya` `xenia` `eugene` |
| `XTTS_SPEAKER_WAV` | — | образец голоса 6–15 с для клонирования (xtts) |
| `VOICE_PITCH` | `1.0` | высота тона; `1.3` для aidar ≈ голос «под Вектора» |
| `VOICE_TEMPO` | `1.0` | скорость речи после питч-шифта |
| `ROBOT_FX` | `false` | лёгкая «цифровая» окраска голоса |
| `ENABLE_EMOTIONS` | `true` | тег `[радость]`/... → анимация перед репликой |
| `TRIGGERS` | `вопрос,спроси,поболтаем,слушай` | чат-слова, вырезаемые из фразы |
| `VOLUME` | `80` | громкость 0–100 (меняется командами «тише/громче») |
| `LISTEN_PORT` | `8090` | порт HTTP API демона |
| `HISTORY_TURNS` / `HISTORY_TTL` | `6` / `300` | память диалога (режим openai) |

Подбор голоса «под Вектора»: `SILERO_SPEAKER=aidar` + `VOICE_PITCH=1.3` +
`VOICE_TEMPO=1.05` + `ROBOT_FX=true`. Приём аутентичный: Anki делали голос
робота именно питч-шифтом со сдвигом формант.

## GPU NVIDIA в LXC (Proxmox) — чек-лист граблей

- Кроме `/dev/nvidia0` и `/dev/nvidiactl` контейнеру нужны **`/dev/nvidia-uvm`**
  и `nvidia-uvm-tools` — без них `nvidia-smi` работает, а CUDA нет, и Ollama
  молча падает на CPU. У uvm **свой major-номер** (не 195!) — его нужно
  разрешить отдельной строкой `lxc.cgroup2.devices.allow: c <major>:* rwm`.
- Если в контейнере uvm-файлы нулевого размера без флага `c` — это пустышки:
  на момент старта контейнера устройств не было на хосте. На хосте:
  `nvidia-modprobe -u -c=0`, затем рестарт контейнера. Добавьте это в
  автозагрузку хоста (systemd-юнит с `Before=pve-guests.service`).
- После старта контейнера CUDA может требовать «прогрева» — добавьте в юнит
  Ollama: `ExecStartPre=-/usr/bin/python3 -c "import ctypes; ctypes.CDLL('libcuda.so.1').cuInit(0)"`.
- Версия userspace-библиотек (`libcuda.so`) в контейнере должна совпадать
  с драйвером хоста. RTX 50xx требует свежие драйвер (570+) и Ollama.
- `OLLAMA_KEEP_ALIVE=-1` в окружении Ollama — чтобы модель не выгружалась.
- Диагностика: `ollama ps` (PROCESSOR должен быть `100% GPU`),
  `journalctl -u ollama | grep "inference compute"`.

## Troubleshooting

| Симптом | Причина / решение |
|---|---|
| `EOFError` при загрузке Silero под systemd | нет `trust_repo=True` в torch.hub.load (в актуальной версии демона исправлено) |
| `exec format error` на trigger.sh | CRLF-переводы строк: `sed -i 's/\r$//'` |
| В логе wire-pod `Executing: ... !speechText !botSerial` буквально | аргументы интента введены через пробел; нужно через запятую |
| `ListAnimations request timed out` | предзагрузка списка анимаций; в актуальной версии отключена (ленивый кэш) |
| Робот молчит, curl-тест работает | смотреть лог wire-pod: дошло ли до `Executing`, и лог демона: пришёл ли POST |
| `StatusCode.UNAUTHENTICATED: 401 Failed to authenticate request` (особенно после ребута/отключения света) | guid в `~/.anki_vector/sdk_config.ini` на хосте демона устарел — wire-pod и демон разошлись. Разовая правка: см. «Если сертификат утерян» (раздел 3, guid — из `botSdkInfo.json`). Чтобы не повторялось: раздел 3.1 (общий bind-mount вместо копирования) |
| Ответы медленные (20+ с) | LLM на CPU — см. раздел про GPU |
| LLM «включила» свет, но нет | модель без инструментов галлюцинирует успех — см. раздел HA |
| Vector-X1D1 не резолвится / таймауты подключения | сменился IP робота — обновить `ip` в sdk_config.ini, закрепить IP в DHCP |

## Известные ограничения

- Рот и idle-анимации робота не синхронизируются с внешним звуком: анимация
  эмоции играет перед репликой, сама речь идёт «из динамика».
- Wake word — только английский «Hey Vector» (прошивка).
- Режим продолжения диалога без wake word (прослушка микрофонов через gRPC
  `AudioFeed`) — экспериментальный и по умолчанию выключен: поток с микрофонов
  приходит в валидном формате (15625 Гц, поле №5, схемы SDK-форка и прошивки
  расходятся — в коде есть ручной парсер), но с околонулевым уровнем сигнала.
  Включить активный режим микрофонов извне нельзя: `AudioSendModeRequest`
  объявлен в protobuf, но не привязан ни к одному gRPC-методу внешнего API.
  Код оставлен в демоне (`FOLLOWUP=true` + модель Vosk) для будущих попыток.
- Таймеры живут в памяти демона и сбрасываются при его рестарте.

## Благодарности

- [kercre123/wire-pod](https://github.com/kercre123/wire-pod) — жизнь после смерти серверов Anki
- [snakers4/silero-models](https://github.com/snakers4/silero-models) — лучший русский TTS для CPU
- [alphacep/vosk](https://alphacephei.com/vosk/) — локальный русский STT
- wirepod-vector-sdk — форк Python SDK Anki для wire-pod
