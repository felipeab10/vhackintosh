# Recuperação local do acesso ao painel

O painel armazena a senha administrativa como hash Argon2id e guarda somente hashes dos tokens opacos de sessão. Para recuperar uma conta, acesse um terminal local do appliance como `root` e execute:

```sh
vhackintosh-web-recover
```

O comando exige a confirmação literal `RECUPERAR`, solicita a nova senha duas vezes e recusa execução sem terminal, por usuário sem privilégio ou por sessão SSH. Ele altera apenas a senha do administrador existente e revoga todas as sessões web. Os discos, catálogos de VM e chaves TLS não são modificados. Se nenhum administrador existir, use `vhackintosh-web-admin setup` no console local.

## Primeiro acesso

O painel não recebe mais um código de configuração no navegador. Para evitar que qualquer pessoa na rede possa reivindicar a primeira conta, crie-a uma única vez no console local do appliance como `root`:

```sh
vhackintosh-web-admin setup
```

O comando solicita usuário e senha duas vezes, exige senha com pelo menos 12 caracteres e recusa sessões SSH. Depois, entre no painel com essas credenciais. A ativação do produto é opcional e pode ser solicitada após o login.
