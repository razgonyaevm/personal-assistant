FROM python:3.12-slim-bookworm

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    jq \
    libicu72 \
    libssl3 \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем PowerShell 7.6 LTS (подходит и для Apple Silicon arm64, и для amd64)
RUN PS_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo "arm64" || echo "x64") && \
    curl -fSL "https://github.com/PowerShell/PowerShell/releases/download/v7.6.10/powershell-7.6.10-linux-${PS_ARCH}.tar.gz" -o /tmp/powershell.tar.gz && \
    mkdir -p /opt/microsoft/powershell/7 && \
    tar zxf /tmp/powershell.tar.gz -C /opt/microsoft/powershell/7 && \
    chmod +x /opt/microsoft/powershell/7/pwsh && \
    ln -s /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh && \
    rm -f /tmp/powershell.tar.gz

WORKDIR /workspace
CMD ["sleep", "infinity"]