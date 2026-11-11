# Linux/amd64 candidates only. This recipe has no deployment credentials.
ARG BUILDER
ARG RUNTIME
FROM ${BUILDER} AS build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /source
COPY artifacts/mcp-source/ /source/
RUN test "$(go env GOVERSION)" = go1.27.3
WORKDIR /source/gateway
RUN xargs go get < /source/gateway-modules.txt && go mod tidy \
    && CGO_ENABLED=1 go test -mod=readonly -count=1 ./... \
    && CGO_ENABLED=1 go test -mod=readonly -race -count=1 ./pkg/proxy ./pkg/idp \
    && go build -mod=readonly -trimpath -buildvcs=false -ldflags '-w -s -buildid=' -o /out/mcp-auth-proxy .
WORKDIR /source/panel
RUN go test -mod=readonly -count=1 ./... \
    && go build -mod=readonly -trimpath -buildvcs=false -ldflags '-w -s -buildid=' -o /out/mcp-1panel .
RUN mkdir /out/locks && cp /source/gateway/go.mod /out/locks/gateway.go.mod \
    && cp /source/gateway/go.sum /out/locks/gateway.go.sum \
    && cp /source/panel/go.mod /out/locks/panel.go.mod \
    && cp /source/panel/go.sum /out/locks/panel.go.sum \
    && go version -m /out/mcp-auth-proxy > /out/locks/gateway-build.txt \
    && go version -m /out/mcp-1panel > /out/locks/panel-build.txt

FROM scratch AS binaries
COPY --from=build /out/ /

FROM ${RUNTIME} AS gateway
ARG DEBIAN_SNAPSHOT
# A dated Debian snapshot makes package selection stable across reruns. Upgrade
# inherited packages too: installing only curl does not fix Perl/Brotli findings.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ bookworm-updates main\ndeb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/%s/ bookworm-security main\n' "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list \
    && apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 python3-pip nodejs npm \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/mcp-auth-proxy /usr/local/bin/mcp-auth-proxy
COPY --from=build /out/mcp-1panel /usr/local/bin/mcp-1panel
ENV DATA_PATH=/data
ENTRYPOINT ["/usr/local/bin/mcp-auth-proxy"]
