# Бесплатные ТВ-каналы

Плейлист для TiviMate и других IPTV-плееров, собранный из открытых списков [iptv-org](https://github.com/iptv-org/iptv).
Группы: Русские, Сербия и Босния, English (для изучения), Детские, Обучающие, Познавательные, Музыка.

Ссылка для плеера:

```
https://raw.githubusercontent.com/okemake116/iptv-playlist/main/free-channels.m3u
```

## Как обновляется

- **Каждый день** GitHub Actions ([update.yml](.github/workflows/update.yml)) заново собирает список
  и проверяет каждый поток. Мёртвые убираются, новые каналы iptv-org добавляются сами.
- Проверка в GitHub идёт из-за границы, поэтому потоки, которые отказывают только по стране
  (HTTP 400/401/403/451), там не удаляются. Pluto TV, Plex и Samsung TV Plus (через `jmp2.uk`)
  исключены всегда: из России они не работают.
- **Из России** (домашняя сеть) время от времени запускается строгая проверка — она оставляет только
  то, что реально открывается, и записывает остальное в [ru-blocklist.txt](ru-blocklist.txt):

  ```
  python3 build_playlist.py free-channels.m3u --strict --write-blocklist ru-blocklist.txt
  ```

## Файлы

- `build_playlist.py` — сборка и проверка (только стандартная библиотека Python 3).
- `free-channels.m3u` — готовый плейлист.
- `ru-blocklist.txt` — потоки, которые не открылись из России; ежедневная сборка их пропускает.
