FROM docker.m.daocloud.io/library/python:3.13-slim

WORKDIR /app

ENV RES_OPTIONS=inet4
ENV http_proxy="" https_proxy="" HTTP_PROXY="" HTTPS_PROXY="" no_proxy="*"

COPY robot-common/ robot-common/
COPY IdentificationClient/ IdentificationClient/
COPY water/ water/
COPY food/ food/
COPY ident/ ident/
COPY search/ search/
COPY weather/ weather/
COPY cur_time/ cur_time/
COPY server/ server/

RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    ./robot-common \
    ./IdentificationClient \
    ./water \
    ./food \
    ./ident \
    ./search \
    ./weather \
    ./cur_time \
    ./server

EXPOSE 8080

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8088", "--timeout", "200", "server:app"]
