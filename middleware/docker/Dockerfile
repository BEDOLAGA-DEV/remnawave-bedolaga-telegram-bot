FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/hwid-blocker .

FROM alpine:3.20
RUN addgroup -S hwid && adduser -S hwid -G hwid
USER hwid
COPY --from=build /out/hwid-blocker /usr/local/bin/hwid-blocker
ENTRYPOINT ["/usr/local/bin/hwid-blocker"]
