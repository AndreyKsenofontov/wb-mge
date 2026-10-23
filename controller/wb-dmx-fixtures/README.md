# wb-dmx-fixtures

Сервис для контроллера Wiren Board, который превращает профили приборов из
[Open Fixture Library](https://open-fixture-library.org) (OFL, лицензия MIT) в MQTT-устройства
со «смысловыми» контролами (диммер, цвет, pan/tilt, режимы строба, колёса гобо) и пишет
получившиеся DMX-каналы в шлюз WB-MGU по Modbus TCP (регистры 0..511 — каналы вселенной,
1000 — blackout). Карта регистров шлюза описана в [`docs/dmx_plan.md`](../../docs/dmx_plan.md).

Без этого сервиса шлюз можно использовать через шаблон wb-mqtt-serial
(`templates/config-wb-mgu-dmx.json`) — но там каждый канал это просто ползунок 0..255.

## Как это выглядит на контроллере

* `/devices/wb_dmx_fixtures` — служебное устройство: `status`, адрес шлюза, число приборов,
  кнопка `blackout` (гасит все контролы и шлёт blackout в шлюз).
* `/devices/dmx_<id>` — по устройству на прибор из конфига; `<id>` задаётся в конфиге.
  В `meta/error` устройств ставится `w`, пока шлюз недоступен.
* Форма «DMX fixtures (WB-MGU gateway)» в «Настройки → Конфигурационные файлы»
  (`/etc/wb-dmx-fixtures.conf`): адрес шлюза и список приборов — профиль, режим DMX,
  стартовый адрес. Схема формы генерируется из библиотеки профилей, поэтому в выпадающем
  списке только импортированные профили, а режимы — те, что есть у выбранного профиля.
  Сохранение формы перезапускает сервис.

### Карта контролов

Профиль OFL разворачивается в список каналов выбранного режима, каждый канал — в контрол:

| Канал OFL | Контрол WB | Как пишется в DMX |
|---|---|---|
| Одна capability на весь диапазон (Intensity, Focus, Speed …) | `range` 0..255 с именем канала (`dimmer`, `focus` …) | значение в канал; fine-канал, если он есть в режиме, получает младший байт `0` |
| `Pan`, `Tilt` с fine-каналом | `range` 0..65535 | старший байт в грубый канал, младший — в fine |
| `Red`, `Green`, `Blue` одного пикселя | один `rgb`-контрол `color` (`color_<pixel>` для матриц) | три канала |
| Другие `ColorIntensity` (`White`, `Amber`, `UV` …) | `range` 0..255 | значение в канал |
| Канал с несколькими capability (строб, колесо цветов/гобо, макросы, программы) | `<name>_mode` — `value` с `enum`, в веб-интерфейсе выпадающий список с подписями из OFL; плюс `<name>` — `range` 0..100 %, если хотя бы одна capability непрерывна (скорость, угол, температура) | начало диапазона выбранной capability; для непрерывной — точка внутри диапазона по уровню в % |
| `NoFunction` на весь канал | контрола нет | значение по умолчанию из профиля |

Имена контролов — имя канала OFL в snake_case. Порядок в карточке: диммер, цвет, остальные
цветовые каналы, pan, tilt, дальше по порядку каналов режима; последним — текстовый `profile`
(«Q-Spot 40 RGBW · 9-channel · DMX 1–9»). Подписи режимов переводятся на русский по словарю
частых слов (`Open`, `Strobe`, `Slot` …), незнакомые остаются как в OFL.

Что получится для конкретного профиля, можно посмотреть без запуска сервиса:

```bash
wb-dmx-fixtures show stairville/mh-x50 --mode 14ch
```

## Установка

Пакет собирается стандартным для WB способом (архитектуронезависимый, Python):

```bash
cd controller/wb-dmx-fixtures
wbdev ndeb
```

`wbdev` нужен Docker; на машине без него пакет можно собрать в любом Debian/Ubuntu с
`debhelper`, `dh-python`, `python3-setuptools`, `python3-pytest` и `python3-wb-common`
командой `dpkg-buildpackage -us -uc -b`. Установка на контроллере:

```bash
apt install ./wb-dmx-fixtures_*.deb
```

Пакет ставит юнит `wb-dmx-fixtures.service`, пустой конфиг `/etc/wb-dmx-fixtures.conf`
(адрес шлюза не задан — сервис запускается, но пишет в `status`, что шлюз не настроен)
и генерирует схему формы `/var/lib/wb-mqtt-confed/schemas/wb-dmx-fixtures.schema.json`.
Библиотека профилей живёт в `/mnt/data/wb-dmx-fixtures/library/<manufacturer>/<fixture>.json`
и переживает обновление прошивки.

Запуск из исходников без пакета (как на прототипе): скопировать каталог на контроллер,
в юните задать `Environment=PYTHONPATH=<каталог>` и `ExecStart=/usr/bin/python3 <каталог>/bin/wb-dmx-fixtures`.

## Импорт профилей

Ключ прибора — `manufacturer/fixture`, как в URL на open-fixture-library.org
(`https://open-fixture-library.org/cameo/q-spot-40-rgbw` → `cameo/q-spot-40-rgbw`).
Контроллеру нужен доступ к raw.githubusercontent.com:

```bash
wb-dmx-fixtures import cameo/q-spot-40-rgbw stairville/mh-x50
```

Команда скачивает JSON профиля в библиотеку и перегенерирует схему confed —
wb-mqtt-confed подхватывает её сам, форма в веб-интерфейсе обновляется после перезагрузки
страницы. Профиль, положенный в библиотеку руками (свой или отредактированный), подключается
командой `wb-dmx-fixtures update-schema`.

Не поддерживаются матричные профили с блоками `matrixChannels` в режиме (`insert`); такой
профиль отвергается с понятной ошибкой. Пиксели, перечисленные в режиме явно
(`Red 1`, `Green 1` …), работают.

## Конфиг

```json
{
  "gateway": {"host": "192.168.13.172", "port": 502, "unit_id": 1, "write_interval_ms": 25},
  "fixtures": [
    {"id": "par1", "name": "Par 1", "fixture": "cameo/q-spot-40-rgbw", "mode": "9-channel", "address": 1},
    {"id": "head1", "name": "Moving head 1", "fixture": "stairville/mh-x50", "mode": "14ch", "address": 20}
  ]
}
```

`write_interval_ms` — пауза перед записью после изменения, чтобы поток значений от ползунка
схлопнулся в одну запись FC16 на прибор. Ошибки конфига (профиль не в библиотеке, нет такого
режима, пересечение адресов, выход за 512 каналов, повтор `id`) — сервис завершается с кодом 6
и текстом ошибки в `journalctl -u wb-dmx-fixtures`; systemd такой выход не перезапускает
(`RestartPreventExitStatus=6`), после исправления конфига через форму сервис стартует сам.

## Разработка

```bash
cd controller/wb-dmx-fixtures
python3 -m pytest tests -q                   # на машине с pytest
uv run --with pytest python -m pytest tests -q -o addopts=""   # без установленного pytest
```

Тесты не требуют `wb_common` и MQTT: модули `ofl`, `mapping`, `library` чистые, сервис
импортируется только командой запуска. Эталонные профили для тестов лежат в `tests/ofl/`.
