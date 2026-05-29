# docker-nvme-speedtest
Test speed in docker container with different caching options on a synology nas.

## Purpose 
I want to speed up my docker container running a homeassistant images. The homeassistant sqlite database is quite huge and I guess it would benefit from using NVMe SSDs instead of the mechanical HDDs inside the synology nas.

## Options
I bought two 265GB  NVMe SSDs for the synology and you can use this devices in three different flavours:
1. As **Read-Cache**, this is even possible with only one NVMe Disk.
2. As **Read/Write Cache**, you need two NVMe Disks.
3. As **Raid 1 SSD Volume**, this is not supported by Synology for thermal reasons, but it is possible.

## Solution
Setup a docker image with a sqlite test database to compare read and write speed under different hardware constallations.
To compare the speed, I run the test container four times.
1. Without any cache, on HDD.
2. With read cache, on HDD.
2. With read/write cache, on HDD.
2. Directly on the SSD Volume.

## Building the image
```docker build -t speedtest .```

## Usage
```docker run -rm speedtest <prefix>```

The prefix will be used to differenciate the test results.

If you are running the container it will do the test and write a file with the results ```<date>-<prefix>-speedresult.txt```

## Results
**not done, yet**

