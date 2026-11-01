# Painel local conectado ao host

`tools/dev-web-manager.sh` inicia o painel de desenvolvimento no loopback e o
conecta ao catálogo real em `~/.config/vhackintosh/vms.json`. A API continua
exigindo sessão autenticada; o servidor não fica exposto na rede local.

O modo de desenvolvimento usa unidades **systemd de usuário**. Ele não precisa
de `sudo`, não altera as unidades do sistema e não inicia VMs sozinho. Cada VM
iniciada pelo painel fica supervisionada por `vhackintosh-vm@<id>.service` no
gerenciador systemd do usuário atual. O vHackintosh usa o `reims-ctl` instalado
em `~/.cargo/bin` e o QEMU emparelhado em `~/.local/share/reims-ctl`.

Para subir novamente:

```bash
cd ~/vhackintosh
./tools/dev-web-manager.sh
```

Abra `http://127.0.0.1:5173/vms/new`, crie uma VM e acompanhe o provisionamento
em **Tarefas**. Depois que o provisionamento terminar, inicie a VM pela página
dela. A VM `preview-macos` do preview antigo usava um disco fictício e não é
reaproveitada. O catálogo de desenvolvimento é o catálogo real do usuário; os
arquivos de VMs não listadas nele não são importados nem removidos.

Para parar apenas o painel de desenvolvimento:

```bash
systemctl --user disable --now vhackintosh-web-dev.service vhackintosh-vite-dev.service
```

As unidades de VM já iniciadas são independentes e continuam sob controle do
systemd do usuário. Pare-as pela interface antes de desligar o host.
