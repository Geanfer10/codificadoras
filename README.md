# Controle de Horímetro — Codificadoras

Painel do PCM (Goiás Verde) com o status de preventiva das codificadoras, alimentado pelo Monday.com.

- **Board de status:** 📋 Controle de Horímetro Codificadoras (18420324650)
- **Board de histórico:** 📋 Registro de Coleta de Horímetros (18420370319)
- **Atualização:** GitHub Actions a cada 30 min (06h–20h) → `data/dados.json` → GitHub Pages

## Regras
- Horas restantes: Crescente = Frequência PM − Horímetro; Regressivo = Horímetro
- ATRASADO ≤ 0 h · PENDENTE ≤ 200 h · EM DIA > 200 h · SEM LEITURA = horímetro vazio
- Previsão da PM = horas restantes ÷ média real de uso (h/dia, últimos 90 dias do histórico)
- Coleta vencida = mais de 14 dias sem leitura
- Parâmetros ajustáveis no topo de `scripts/buscar_monday.py`
