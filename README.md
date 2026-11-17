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
   wire-pod: распознавание (встроенный Vosk ru-RU или Wyoming-сервер, см. ниже)
        │    + кастомный интент — триггер-слова
        │  exec: trigger.sh "<фраза>" <serial>   (мгновенно уходит в фон)
        ▼
vector_ru_daemon.py (Flask, systemd-сервис)
        ├── локальные команды (таймеры, громкость) — без LLM
        ├── LLM_BACKEND=openai        → OpenAI-совместимый API (llama.cpp, Ollama, LM Studio)
        │   LLM_BACKEND=homeassistant → HA Conversation API (LLM + умный дом)
        ├── тег эмоции [радость]/[грусть]/... → анимация робота
        ├── TTS: Silero v4 (CPU, быстрее реального времени) или XTTS v2
        ├── ffmpeg: питч-шифт "под Вектора" → WAV 16000 Гц / mono / 16-bit
        └── SDK (wirepod-vector-sdk) → динамик робота

Home Assistant ── Wyoming :10200 ──► тот же демон: Silero для колонок HA
```

## Требования

- Anki Vector, переведённый на wire-pod (любая установка: deb, docker, исходники);
- Linux-хост/LXC для демона: 2+ ядра, 4 ГБ RAM (8 ГБ для XTTS), Python 3.10–3.12;
- локальная LLM: llama.cpp (`llama-server`) / Ollama / LM Studio с OpenAI-совместимым API;
- опционально: Home Assistant с LLM-агентом разговора (для умного дома);
- опционально: Wyoming STT-сервер (например, GigaAM v3) — распознавание речи
  для HA и, через форк wire-pod, для самого робота;
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

Большая русская модель Vosk занимает ~6,5 ГБ памяти. Если у вас уже есть
Wyoming STT-сервер, wire-pod можно перевести на него — см. раздел
«Распознавание речи робота через Wyoming».

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

1. HA → Настройки → Устройства и службы → интеграция вашей LLM (Ollama, или
   интеграция с OpenAI-совместимым API для llama.cpp) → Параметры: включить
   **«Управление Home Assistant»**, модель с поддержкой инструментов,
   историю сообщений 5–10.
2. Настройки → Голосовые ассистенты: открыть (expose) нужные устройства,
   задать им русские имена/псевдонимы и помещения. Включить
   **«Предпочитать локальную обработку команд»** — стандартные команды
   («включи свет на кухне») исполняются движком интентов HA мгновенно и
   без LLM; в LLM уходит только то, что не подошло под шаблоны.
3. Профиль → создать долгосрочный токен. Id агента — в Средствах разработчика
   → Действия → `conversation.process` (вида `conversation.xxx`). Если поменяли
   интеграцию LLM, id агента тоже поменяется — его нужно обновить в юните.
4. В `vector-ru.service`: `LLM_BACKEND=homeassistant`, `HA_URL`, `HA_TOKEN`,
   `HA_AGENT_ID`.
5. Системный промпт в этом режиме задаётся в настройках интеграции LLM (поле
   Instructions), демон свой `SYSTEM_PROMPT` в HA не отправляет:

```
Ты — маленький домашний робот Вектор, помощник в умном доме.
Отвечай ТОЛЬКО по-русски, кратко и живо: одно-три коротких предложения.
Никаких списков, markdown, эмодзи и английских слов. Все числа пиши словами.
У тебя любопытный и дружелюбный характер.

Когда нужно управлять устройствами — сначала вызывай инструменты, без тегов и
лишнего текста. И только в ФИНАЛЬНОМ текстовом ответе человеку самым первым
словом поставь тег эмоции в квадратных скобках, один из:
[радость] [грусть] [удивление] [злость] [нейтрально]. После тега сразу идёт ответ.
```

Теги должны быть ровно эти пять: их ищет демон, чтобы сыграть анимацию.
Требовать тег **перед** вызовом инструментов нельзя — это ломает tool calling
у дисциплинированных моделей (Qwen): они пишут текст вместо вызова функции.

Проверка агента: Средства разработчика → Действия → `conversation.process` —
ответ должен начинаться с тега, например `[радость] Привет!`.

**llama.cpp.** `llama-server` нужно запускать с `--jinja`, иначе tool calling
не работает. Умеет ли модель вызывать инструменты, проверяется запросом с
`tools` напрямую в сервер — в ответе должен быть `tool_calls`:

```bash
curl -s http://<llm-хост>:8080/v1/chat/completions -H "Content-Type: application/json" -d '{
  "messages":[{"role":"user","content":"включи свет на кухне"}],
  "tools":[{"type":"function","function":{"name":"turn_on","description":"включить устройство",
    "parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}}]}'
```

Контекст (`-c`) нужен с запасом: в него уходят промпт, список открытых
устройств и история.

При «модель говорит, что включила, но не включила»: убедиться, что включено
«Управление HA» и что модель реально возвращает `tool_calls` (проверка выше);
снизить температуру; сократить историю; не требовать тег эмоции перед вызовом
инструментов. Из моделей для Ollama на 8 ГБ VRAM хорошо себя показали
`qwen3:8b` (лучший баланс русский/инструменты), `qwen3:4b-instruct`,
`qwen2.5:7b`.

### Озвучка самой Home Assistant голосом Silero (Wyoming)

Демон умеет работать и как TTS-сервер для самой HA: тогда обычные колонки
(media_player) говорят тем же Silero, что и робот. Это независимо от голоса
робота — он синтезируется отдельной дорожкой (`synthesize()` в демоне) со своим
`SILERO_SPEAKER` и питч-шифтом.

**Штатный способ — Wyoming** (тот же протокол, что у wyoming-vosk/piper). Демон
поднимает Wyoming-сервер на порту 10200 автоматически при `TTS_ENGINE=silero`.
В HA: Настройки → Интеграции → **Wyoming Protocol** → хост демона и порт `10200`.
Появится TTS-сущность с выбором любого из пяти голосов прямо в UI, работающая
в пайплайнах Assist.

```
Hey Vector → wire-pod → демон → динамик робота   (SILERO_SPEAKER + VOICE_PITCH)
Home Assistant → Wyoming :10200 → демон → колонки (голос из HA + HA_TTS_PITCH)
```

**Высота и скорость.** В протоколе Wyoming полей для них нет — HA передаёт
только голос и язык, поэтому задать их поканально через `tts.speak` нельзя.
Это серверные параметры демона, общие для всей озвучки HA: `HA_TTS_PITCH` и
`HA_TTS_TEMPO` (см. таблицу настроек). Применяются тем же приёмом, что и к
голосу робота: тон сдвигается вместе с формантами, длительность возвращается
обратно через `atempo`. Голос робота они не затрагивают — у него свои
`VOICE_PITCH`/`VOICE_TEMPO`.

**Legacy-способ — marytts.** Endpoint `/process` на порту демона остаётся для
совместимости (и удобен для прослушивания голосов через curl), но интеграция
`marytts` в HA помечена как legacy, не создаёт TTS-сущность и однажды будет
удалена. Если она у вас ещё настроена:

```yaml
tts:
  - platform: marytts
    host: <IP демона>
    port: 8090
    codec: WAVE_FILE
    voice: xenia          # aidar|baya|kseniya|xenia|eugene
    language: ru
```

Прослушать голоса, не трогая HA:

```bash
for v in aidar baya kseniya xenia eugene; do
  curl -s -X POST http://127.0.0.1:8090/process \
    --data-urlencode "INPUT_TEXT=Привет, это тест голоса" \
    --data-urlencode "VOICE=$v" -o "/tmp/voice_$v.wav"
done
```

## Сервер распознавания речи: GigaAM v3 через Wyoming

Для колонок HA (и робота, см. следующий раздел) удобно держать один Wyoming
STT-сервер. Для русского хорошо подходит [GigaAM v3](https://github.com/salute-developers/GigaAM)
в ONNX-варианте: на 8 ядрах Xeon E5-2680 v4 без GPU фраза в 1,8 с
распознаётся за 0,3–0,4 с.

| Модель | Фраза 1,8 с | Результат |
|---|---|---|
| Whisper `small-int8`, beam 1 | 2,3 с | «Проверка скорости и высоты голоса.» |
| Whisper `medium-int8`, beam 1 | 6,0 с | «Проверка скорости и высоты голоса» |
| **GigaAM `v3-rnnt`** | **0,3–0,4 с** | «проверка скорости и высоты голоса» |

Сервер — тот же `wyoming-faster-whisper`, с библиотекой
[onnx-asr](https://github.com/istupakov/onnx-asr) вместо faster-whisper:

```bash
python3 -m venv /opt/whisper
/opt/whisper/bin/pip install wyoming-faster-whisper "onnx-asr[cpu,hub]==0.11.0"
```

`/etc/systemd/system/wyoming-whisper.service`:

```ini
[Unit]
Description=Wyoming STT (GigaAM v3 via onnx-asr)
After=network-online.target

[Service]
Environment=HF_HUB_OFFLINE=1
ExecStart=/opt/whisper/bin/wyoming-faster-whisper --stt-library onnx-asr \
  --model gigaam-v3-rnnt --language ru --vad-clip onnx-asr --local-files-only \
  --uri tcp://0.0.0.0:10300 --data-dir /opt/whisper/data
Restart=always

[Install]
WantedBy=multi-user.target
```

Модель (~0,9 ГБ, репозиторий `istupakov/gigaam-v3-onnx`) скачивается в
`--data-dir` при первом запуске. Для этого первый раз запустите службу **без**
`HF_HUB_OFFLINE=1` и `--local-files-only`, дождитесь загрузки, потом верните
их — дальше всё работает офлайн. Процесс занимает ~1,4 ГБ RAM. В HA сервер
подключается так же, как TTS: интеграция **Wyoming Protocol**, порт `10300`.

Что учесть:

- **Шум.** `--vad-clip onnx-asr` обязателен: без обрезки тишины длинная шумная
  запись распознаётся в пустую строку. Короткие команды с колонок
  распознаются нормально. Whisper без VAD «сочинял» команды из шума — это и
  было причиной перехода.
- **Формат вывода:** строчные буквы, без пунктуации, «ё» → «е», числа словами.
  Устройства с «ё» в названии («тёплый пол») надёжнее снабдить в HA
  псевдонимом через «е».
- Вариант `v3-e2e-rnnt` (с пунктуацией) в тестах ошибался чаще — берите
  `v3-rnnt`.
- `pthread_setaffinity_np failed ... error code: 22` в журнале — onnxruntime в
  LXC, на работу не влияет.

## Распознавание речи робота через Wyoming (GigaAM вместо Vosk)

Штатный wire-pod распознаёт речь встроенным Vosk. Большая русская модель 0.42
держит в памяти ~6,5 ГБ (на контейнере с 6 ГБ — наполовину в свопе) и пишет
«ё», которой нет в выводе других движков. Форк
**[WiRe-gd/wire-pod, ветка `wyoming`](https://github.com/WiRe-gd/wire-pod/tree/wyoming)**
добавляет провайдер **Wyoming**: робот отдаёт звук тому же Wyoming STT-серверу,
что и Home Assistant (например, GigaAM v3 через `wyoming-faster-whisper
--stt-library onnx-asr`). Модель одна на двоих, wire-pod занимает ~35 МБ, фраза
распознаётся за 0,15–0,5 с.

Штатно так сделать нельзя: движок распознавания вкомпилирован в бинарник
wire-pod, клиента Wyoming в нём нет, а провайдер `whisper` ходит только на
api.openai.com.

Что меняет форк (по коммиту на пункт):

- провайдер `stt/wyoming`; если сервер недоступен, wire-pod всё равно стартует;
- настройки в веб-интерфейсе: Server Settings → STT Language → блок «Wyoming
  server» (адрес, модель, язык, таймаут) и кнопка **Test connection**;
- исправлено: wire-pod принудительно ставил `en-US` любому провайдеру, кроме
  vosk/whisper, — русские интенты перестали бы работать;
- сопоставление интентов: «ё» = «е» при сравнении; короткие ключевые слова
  («да», «нет», «дом») засчитываются только целыми словами; **точная встроенная
  фраза проверяется раньше пользовательских интентов** — иначе «как» в
  `russian_chat` перехватывало «как меня зовут».

**Сборка и установка** — в
[`wyoming-build/README.md`](https://github.com/WiRe-gd/wire-pod/blob/wyoming/wyoming-build/README.md)
форка. Коротко:

```bash
git clone -b wyoming https://github.com/WiRe-gd/wire-pod.git
wire-pod/wyoming-build/build.sh /opt/build      # бинарник и webroot в /opt/build/out
```

Скрипт собирает бинарник так же, как официальный .deb, и печатает, какая glibc
ему нужна: она не должна быть новее, чем на хосте wire-pod (сборка на Ubuntu
24.04 требует 2.34, Debian 12 даёт 2.36). Ставится он рядом со штатным
`/usr/bin/wire-pod` и включается drop-in'ом systemd, поэтому откат — удалить
drop-in и вернуть старый `webroot`.

Две вещи, о которых легко забыть:

- в drop-in нужен `WYOMING_STT_ADDRESS` (пока адрес не сохранён в
  веб-интерфейсе), а если веб-интерфейс открывается через свой домен —
  `WIREPOD_ALLOWED_HOSTS` и `WIREPOD_ALLOWED_ORIGINS`: wire-pod 1.6.x отклоняет
  запросы с публичных доменов;
- если Wyoming-сервер упадёт, робот перестанет понимать речь вообще, а не только
  русские ответы LLM. Встроенные ответы Вектора, где он говорит (имя, погода),
  прошивка произносит по-английски.

## Справочник настроек (`vector-ru.service`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LLM_BACKEND` | `openai` | `openai` (LLM напрямую) / `homeassistant` |
| `LLM_URL` | `http://127.0.0.1:11434/v1/chat/completions` | endpoint LLM (по умолчанию Ollama; для `llama-server` — `http://<хост>:8080/v1/chat/completions`) |
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
| `WYOMING_ENABLE` | `true` | Wyoming TTS-сервер для HA (только при `TTS_ENGINE=silero`) |
| `WYOMING_PORT` | `10200` | порт Wyoming-сервера |
| `WYOMING_RATE` | `48000` | частота синтеза для HA: `8000` / `24000` / `48000` |
| `HA_TTS_PITCH` | `1.0` | высота тона озвучки HA (робота не касается) |
| `HA_TTS_TEMPO` | `1.0` | скорость речи озвучки HA; `1.1` — чуть бодрее, `0.9` — медленнее |

Подбор голоса «под Вектора»: `SILERO_SPEAKER=aidar` + `VOICE_PITCH=1.3` +
`VOICE_TEMPO=1.05` + `ROBOT_FX=true`. Приём аутентичный: Anki делали голос
робота именно питч-шифтом со сдвигом формант.

## GPU NVIDIA в LXC (Proxmox) — чек-лист граблей

- Кроме `/dev/nvidia0` и `/dev/nvidiactl` контейнеру нужны **`/dev/nvidia-uvm`**
  и `nvidia-uvm-tools` — без них `nvidia-smi` работает, а CUDA нет, и
  Ollama / llama.cpp молча уходят на CPU. У uvm **свой major-номер** (не 195!) — его нужно
  разрешить отдельной строкой `lxc.cgroup2.devices.allow: c <major>:* rwm`.
- Если в контейнере uvm-файлы нулевого размера без флага `c` — это пустышки:
  на момент старта контейнера устройств не было на хосте. На хосте:
  `nvidia-modprobe -u -c=0`, затем рестарт контейнера. Добавьте это в
  автозагрузку хоста (systemd-юнит с `Before=pve-guests.service`).
- После старта контейнера CUDA может требовать «прогрева» — добавьте в юнит
  LLM-сервера (Ollama, `llama-server`): `ExecStartPre=-/usr/bin/python3 -c "import ctypes; ctypes.CDLL('libcuda.so.1').cuInit(0)"`.
- Версия userspace-библиотек (`libcuda.so`) в контейнере должна совпадать
  с драйвером хоста. RTX 50xx требует свежие драйвер (570+) и Ollama.
- `OLLAMA_KEEP_ALIVE=-1` в окружении Ollama — чтобы модель не выгружалась.
- Диагностика: `ollama ps` (PROCESSOR должен быть `100% GPU`),
  `journalctl -u ollama | grep "inference compute"`; для `llama-server` —
  процесс в `nvidia-smi` и занятая им видеопамять.

## Troubleshooting

| Симптом | Причина / решение |
|---|---|
| `EOFError` при загрузке Silero под systemd | нет `trust_repo=True` в torch.hub.load (в актуальной версии демона исправлено) |
| `exec format error` на trigger.sh | CRLF-переводы строк: `sed -i 's/\r$//'` |
| В логе wire-pod `Executing: ... !speechText !botSerial` буквально | аргументы интента введены через пробел; нужно через запятую |
| `ListAnimations request timed out` | предзагрузка списка анимаций; в актуальной версии отключена (ленивый кэш) |
| Робот молчит, curl-тест работает | смотреть лог wire-pod: дошло ли до `Executing`, и лог демона: пришёл ли POST |
| После обновления wire-pod веб-интерфейс по домену отвечает «forbidden host» или «cross-origin request blocked» | в wire-pod 1.6.x появился webguard: домены на публичных зонах нужно перечислить в `WIREPOD_ALLOWED_HOSTS` / `WIREPOD_ALLOWED_ORIGINS` (см. раздел про Wyoming для робота) |
| Встроенные команды Вектора («как меня зовут», «дай пять») уходят в LLM | пользовательский интент с общими словами («как», «расскажи») проверяется раньше встроенных; в [форке с Wyoming](https://github.com/WiRe-gd/wire-pod/tree/wyoming) точные встроенные фразы проверяются первыми, в штатном wire-pod — убрать общие слова из utterances |
| HA не находит Wyoming-сервис на порту 10200 | демон поднимает его только при `TTS_ENGINE=silero` и `WYOMING_ENABLE=true`; проверить в логе строку `Wyoming TTS на порту 10200` и что порт не занят другим wyoming-сервисом (vosk/piper) |
| Интеграция Wyoming добавилась, голоса в HA видны, но звука нет | в логе демона смотреть, доходит ли до строки `Wyoming: синтез голосом ...`. Если видно только `событие synthesize` — значит клиент шлёт данные отдельным блоком (`data_length`), а не внутри строки заголовка; в актуальной версии демона это учтено |
| `StatusCode.UNAUTHENTICATED: 401 Failed to authenticate request` (особенно после ребута/отключения света) | guid в `~/.anki_vector/sdk_config.ini` на хосте демона устарел — wire-pod и демон разошлись. Разовая правка: см. «Если сертификат утерян» (раздел 3, guid — из `botSdkInfo.json`). Чтобы не повторялось: раздел 3.1 (общий bind-mount вместо копирования) |
| Ответы медленные (20+ с) | LLM на CPU — см. раздел про GPU |
| LLM «включила» свет, но нет | модель без инструментов галлюцинирует успех — см. раздел HA |
| Vector-XXXX не резолвится / таймауты подключения | сменился IP робота — обновить `ip` в sdk_config.ini, закрепить IP в DHCP |

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
- [wirepod-vector-sdk](https://pypi.org/project/wirepod-vector-sdk/) — форк Python SDK Anki для wire-pod
- [snakers4/silero-models](https://github.com/snakers4/silero-models) — лучший русский TTS для CPU
- [salute-developers/GigaAM](https://github.com/salute-developers/GigaAM) — быстрое и точное распознавание русской речи
- [istupakov/onnx-asr](https://github.com/istupakov/onnx-asr) — GigaAM в ONNX без PyTorch
- [rhasspy/wyoming-faster-whisper](https://github.com/rhasspy/wyoming-faster-whisper) и [протокол Wyoming](https://github.com/OHF-Voice/wyoming) — общий язык голосовых сервисов с Home Assistant
- [alphacep/vosk](https://alphacephei.com/vosk/) — локальный русский STT
- [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) и [Ollama](https://ollama.com) — локальные LLM
- [Home Assistant](https://www.home-assistant.io) — умный дом, которым управляет робот
