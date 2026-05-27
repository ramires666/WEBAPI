#!/bin/bash -i
echo "Переходим в папку..."
cd /mnt/w/_python/APIPROXY
sleep 1

echo "Активируем conda btc..."
conda activate btc
sleep 1

echo "Запускаем питон..."
# Here we use python with -c to execute "commands" with 1 second delay
python -c "
import time
print('Python запущен. Ждем 1 секунду...')
time.sleep(1)
import legacy.main as main
import asyncio
asyncio.run(main.main())
"
