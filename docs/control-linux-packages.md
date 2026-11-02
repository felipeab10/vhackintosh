# Pacotes Linux do vHackintosh Control

O script `packaging/build-all.sh` compila o frontend uma vez, monta o payload
comum e cria pacotes para três ambientes x86-64 em containers:

- Ubuntu 24.04 LTS: `.deb`
- Fedora 44: `.rpm`
- Arch Linux: `.pkg.tar.zst`

## Construir os três pacotes

No host de desenvolvimento, instale Node.js 22, Python 3.12 ou superior, `uv`,
`rsync` e Podman ou Docker. Depois execute:

```bash
packaging/build-all.sh 1.0.0-beta.2 dist
```

O primeiro argumento é a versão e o segundo é o diretório de saída. Se forem
omitidos, o script usa `1.0.0` e `dist/`. A engine pode ser escolhida com
`VHACK_CONTAINER_ENGINE=podman` ou `VHACK_CONTAINER_ENGINE=docker`.

Cada pacote é montado dentro de uma imagem limpa da sua distribuição. Os
containers são usados apenas para empacotar; o frontend e as dependências
Python são preparados no host de build e compartilhados como um payload. O
script valida os metadados de cada artefato e grava `SHA256SUMS` no diretório
de saída.

## Gerar e publicar a próxima release beta

Depois que o workflow dos pacotes estiver integrado à branch padrão, gere os
três pacotes e publique a próxima tag beta em uma execução:

```bash
packaging/build-all.sh --publish
```

O script busca as tags de `origin` e incrementa o número beta mais recente
(por exemplo, `control-v1.0.0-beta.1` vira `control-v1.0.0-beta.2`). Após os
três pacotes serem gerados com sucesso, cria uma tag anotada e a envia ao
GitHub. O Actions reconstrói os pacotes e publica a release quando as
validações terminarem. A publicação exige checkout limpo e sincronizado com a
branch padrão. O comando normal de build continua sem publicar nada.

Para fixar versão e diretório de saída:

```bash
packaging/build-all.sh --publish --version 1.0.0-beta.2 --output dist
```

Depois de uma release estável, a próxima versão automática será o patch
seguinte com sufixo `-beta.1`. Tags existentes nunca são sobrescritas.

Para uma versão com sufixo de pré-lançamento, Debian preserva o hífen; RPM e
Arch convertem o hífen em ponto, conforme o formato de versão aceito por esses
gerenciadores.

## Instalar

```bash
# Ubuntu
sudo apt install ./vhackintosh-control_<versão>_amd64.deb

# Fedora
sudo dnf install ./vhackintosh-control-<versão>-1.fc44.x86_64.rpm

# Arch Linux
sudo pacman -U ./vhackintosh-control-<versão>-1-x86_64.pkg.tar.zst
```

Após instalar, abra `https://localhost:8443`. O primeiro acesso usa `admin` /
`vhackintosh`, pede uma senha nova e então permite selecionar a release do
Reims vGPU. O pacote Control não inclui o runtime Reims.

As releases são voltadas a hosts x86-64 com systemd. Iniciar uma VM também
requer virtualização KVM disponível e GPU/driver Vulkan compatíveis. Construir
e validar os pacotes não substitui o teste da VM no hardware do usuário.
