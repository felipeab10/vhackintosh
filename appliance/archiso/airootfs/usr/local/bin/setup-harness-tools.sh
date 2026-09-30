#!/usr/bin/env bash
# ==============================================================================
# vHackintosh Appliance - AI Coding Harness Setup & Updater
# Instala e atualiza os principais Harnesses CLI para automação e coding:
#   - Claude Code CLI (@anthropic-ai/claude-code)
#   - Codex CLI (@openai/codex)
#   - OpenCode CLI (opencode-cli)
# ==============================================================================

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}======================================================${NC}"
echo -e "${CYAN}      vHackintosh AI Harness Tools Installer          ${NC}"
echo -e "${CYAN}======================================================${NC}\n"

# Verifica se há conectividade de rede
if ! ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
    echo -e "${YELLOW}Aviso: Conexão com a Internet não detectada.${NC}"
    echo -e "Conecte-se à rede via Ethernet ou 'nmtui' antes de rodar o instalador de harnesses."
    exit 1
fi

echo -e "${BLUE}▶ Atualizando ferramentas globais do Node.js/NPM...${NC}"
npm config set fund false
npm config set audit false

echo -e "${BLUE}▶ Instalando Claude Code CLI (@anthropic-ai/claude-code)...${NC}"
npm install -g @anthropic-ai/claude-code

echo -e "${BLUE}▶ Instalando OpenAI Codex CLI (@openai/codex)...${NC}"
npm install -g @openai/codex

echo -e "${BLUE}▶ Instalando OpenCode CLI (opencode-cli)...${NC}"
npm install -g opencode-cli

echo -e "\n${GREEN}✔ Todos os Harnesses CLI foram configurados com sucesso!${NC}\n"
echo -e "Ferramentas disponíveis no terminal:"
echo -e "  • ${CYAN}claude${NC}    -> Claude Code CLI"
echo -e "  • ${CYAN}codex${NC}     -> OpenAI Codex CLI"
echo -e "  • ${CYAN}opencode${NC}  -> OpenCode CLI"
echo -e "\nExecute qualquer um dos comandos acima para iniciar sua sessão de AI coding."
