# app.py
from flask import Flask, jsonify, request, render_template, send_file
import threading, time, random, csv, os
from datetime import datetime

# --- 1. CONFIGURAÇÃO DO SERVIDOR WEB (FLASK) ----
# Aqui iniciamos o Flask, que é o "cérebro" do site.
app = Flask(__name__)

# Estas configurações forçam o site a recarregar se fizermos mudanças no HTML,
# útil para desenvolvimento sem precisar reiniciar o servidor toda hora.
app.config['TEMPLATES_AUTO_RELOAD'] = True 
app.jinja_env.auto_reload = True

# --- 2. ESTADO GLOBAL DO SISTEMA (A "MEMÓRIA" DA ESTUFA) ---
# Este dicionário 'state' guarda tudo o que está acontecendo AGORA.
# É compartilhado entre a simulação (backend) e o site (frontend).
state = {
    "temperatura": 25.0,     # Leitura atual do sensor de temperatura (°C)
    "umidade": 60.0,         # Leitura atual do sensor de umidade do ar (%)
    "soil_moisture": 50.0,   # Leitura atual do sensor de umidade do solo (%)
    "aquecedor": False,      # Estado do atuador: True (Ligado) ou False (Desligado)
    "ventilador": False,     # Estado do atuador: True (Ligado) ou False (Desligado)
    "pump": False,           # Estado da bomba de água
    "pump_run_seconds": 0,   # Contador de segurança: quanto tempo a bomba está ligada direto
    "modo_auto": True,       # Se True, o computador decide. Se False, o usuário clica nos botões.
    "alarm": "",             # Mensagem de erro para exibir no topo do site (ex: "Temp Alta!")
    "pid_output": 0,         # Valor calculado pelo algoritmo PID (apenas para visualização)
    "setpoint": 25.0         # A meta: qual temperatura queremos manter?
}

# --- 3. PARÂMETROS DE TEMPO E CONTROLE ---
DT = 1.0                  # "Delta Time": Quanto tempo (segundos) passa a cada ciclo do loop.
SETPOINT_TEMP = 25.0      # Meta de temperatura desejada. O PID tentará chegar aqui.

# --- 4. CONFIGURAÇÃO DO ALGORITMO PID ---
# O PID é uma fórmula matemática para controle suave e preciso.
# Kp (Proporcional): A "força bruta". Se o erro é grande, a reação é grande.
Kp = 10.0   
# Ki (Integral): A "memória". Corrige pequenos erros que persistem ao longo do tempo.
Ki = 0.2    
# Kd (Derivativo): O "freio". Percebe se a temperatura está mudando rápido demais e suaviza.
Kd = 5.0

# Variáveis internas para o cálculo do PID (não mexer manualmente)
pid_integral = 0.0        # Acumulador de erros passados
pid_last_error = 0.0      # O erro da medição anterior (para calcular a velocidade de mudança)

# --- 5. CONFIGURAÇÃO DO PWM (PULSE WIDTH MODULATION) ---
# Como o aquecedor é digital (só liga ou desliga), usamos PWM para simular potência.
# Ex: Para 30% de força, ligamos por 3 segundos e desligamos por 7 segundos.
PWM_PERIOD = 10.0         # Tamanho total do ciclo em segundos
pwm_counter = 0.0         # Contador interno para saber em qual segundo do ciclo estamos

# --- 6. LIMITES E SEGURANÇA ---
SOIL_LOW = 40.0           # Se o solo cair abaixo disso, liga a bomba.
SOIL_HIGH = 60.0          # Se o solo passar disso, desliga a bomba.
MAX_PUMP_SECONDS = 600    # Segurança: desliga a bomba se ficar ligada por 10 minutos (evita queimar).

# --- 7. FÍSICA DA SIMULAÇÃO (REGRAS DO MUNDO REAL) ---
# Estas variáveis definem como a "natureza" se comporta dentro do código.
PUMP_RATE = 0.6           # Quanta água a bomba joga no solo por segundo (%).
EVAP_BASE = 0.02          # Evaporação mínima que sempre acontece, mesmo no frio.
AIR_DRYING_FACTOR = 0.005 # Quanto o ar seca a cada grau de temperatura (ar quente retém mais água).
SOIL_EVAP_FACTOR = 0.005  # Quanto o calor faz a água do solo evaporar.
SOIL_TO_AIR_TRANSFER = 0.4 # CICLO DA ÁGUA: 40% da água que sai do solo vira vapor e aumenta a umidade do ar.

# --- 8. SISTEMA DE ARQUIVO (HISTÓRICO) ---
HISTORY_FILE = "historico.csv"
# Se o arquivo não existe, criamos ele agora e escrevemos o cabeçalho (títulos das colunas).
if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp","temperatura","umidade","soil_moisture","aquecedor","ventilador","pump","alarm"])

def append_history():
    """
    Função auxiliar que pega o estado atual e salva uma linha no arquivo CSV.
    Isso permite gerar gráficos históricos depois.
    """
    try:
        with open(HISTORY_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(), # Data e hora atual
                state["temperatura"],
                state["umidade"],
                state["soil_moisture"],
                int(state["aquecedor"]),    # Converte True/False para 1/0
                int(state["ventilador"]),
                int(state["pump"]),
                state["alarm"]
            ])
    except Exception as e:
        print(f"Erro ao gravar CSV: {e}")

# --- 9. FUNÇÃO MATEMÁTICA DO PID ---
def calcular_pid(temp_atual):
    """
    Recebe a temperatura atual e decide 'quanto' esforço precisamos fazer.
    Retorno positivo: Precisa Aquecer.
    Retorno negativo: Precisa Resfriar.
    """
    global pid_integral, pid_last_error
    
    # Passo 1: Calcular o ERRO (Onde quero estar - Onde estou)
    erro = SETPOINT_TEMP - temp_atual
    
    # Passo 2: Termo Proporcional (P)
    # Reação imediata ao tamanho do erro.
    P = Kp * erro
    
    # Passo 3: Termo Integral (I)
    # Acumula o erro ao longo do tempo. Se o erro persiste, o I cresce para forçar a correção.
    pid_integral += erro * DT
    # "Anti-windup": Limitamos o acumulador para ele não crescer infinitamente e travar o sistema.
    pid_integral = max(min(pid_integral, 50), -50) 
    I = Ki * pid_integral
    
    # Passo 4: Termo Derivativo (D)
    # Calcula a velocidade da mudança (Erro atual - Erro anterior).
    # Serve para frear o sistema se ele estiver indo rápido demais em direção à meta.
    derivative = (erro - pid_last_error) / DT
    D = Kd * derivative
    
    # Atualiza o erro anterior para o próximo ciclo usar
    pid_last_error = erro
    
    # Soma tudo para ter a saída final
    return P + I + D

# --- 10. LOOP PRINCIPAL DE SIMULAÇÃO ---
def simular():
    """
    Esta função roda em paralelo (thread) eternamente.
    Ela faz duas coisas:
    1. Atua como o 'Cérebro' (Controlador): Liga/Desliga coisas baseado nos sensores.
    2. Atua como a 'Natureza' (Física): Simula a temperatura subindo/descendo e a água secando.
    """
    global pwm_counter
    
    while True:
        # --- PARTE A: CÉREBRO (CONTROLE AUTOMÁTICO) ---
        if state["modo_auto"]:
            
            # 1. Calcula o PID para saber a "força" necessária
            pid_out = calcular_pid(state["temperatura"])
            state["pid_output"] = round(pid_out, 2)

            # 2. Aplica PWM (Transforma força analógica em pulsos digitais ON/OFF)
            # Duty Cycle é a porcentagem de tempo que o aquecedor fica ligado no ciclo.
            duty_cycle = min(abs(pid_out), 100.0) # Limita em 100%
            
            # Verifica se no segundo atual do ciclo o aparelho deve estar ligado
            is_active_cycle = (pwm_counter * 10) < duty_cycle

            if pid_out > 0: 
                # Se o PID for positivo, precisamos de CALOR
                state["aquecedor"] = is_active_cycle
                state["ventilador"] = False
            else: 
                # Se o PID for negativo, precisamos RESFRIAR (Ventilador)
                state["aquecedor"] = False
                state["ventilador"] = is_active_cycle
            
            # Avança o contador do ciclo PWM (0, 1, 2 ... 9, 0, 1 ...)
            pwm_counter = (pwm_counter + 1) % (PWM_PERIOD / DT)

            # 3. Controle de Água (Lógica Simples de Liga/Desliga com margem)
            if state["soil_moisture"] < SOIL_LOW and not state["pump"]:
                # Se está muito seco e a bomba está desligada, liga.
                if state["pump_run_seconds"] < MAX_PUMP_SECONDS:
                    state["pump"] = True
            elif state["soil_moisture"] >= SOIL_HIGH and state["pump"]:
                # Se já está úmido o suficiente, desliga.
                state["pump"] = False

        # --- PARTE B: NATUREZA (SIMULAÇÃO FÍSICA) ---
        
        # 1. Física da Temperatura
        temp = state["temperatura"]
        if state["aquecedor"]:
            temp += 0.5 * DT  # Aquecedor sobe a temperatura
        elif state["ventilador"]:
            temp -= 0.4 * DT  # Ventilador baixa a temperatura
            
        # Perda térmica (Inércia): A temperatura tende a voltar lentamente para 20°C (ambiente externo)
        temp -= (temp - 20.0) * 0.05 * DT 
        # Adiciona um pequeno ruído aleatório para parecer um sensor real
        temp += random.uniform(-0.05, 0.05) * DT
        # Salva garantindo limites (0 a 60 graus)
        state["temperatura"] = round(max(0.0, min(60.0, temp)), 2)

        # 2. Física da Água (Solo -> Ar)
        soil = state["soil_moisture"]
        
        # Taxa de evaporação: Quanto mais quente, mais água evapora do solo.
        evaporation_rate = EVAP_BASE + (state["temperatura"] * SOIL_EVAP_FACTOR)
        water_evaporated = evaporation_rate * DT # Quantidade exata evaporada neste segundo
        
        # Retira a água do solo
        soil -= water_evaporated
        
        # Se a bomba estiver ligada, adiciona água ao solo
        if state["pump"]:
            soil += PUMP_RATE * DT
            state["pump_run_seconds"] += 1 # Conta tempo de segurança
        else:
            state["pump_run_seconds"] = 0
            
        state["soil_moisture"] = round(max(0.0, min(100.0, soil)), 2)

        # 3. Física da Umidade do Ar
        hum = state["umidade"]
        
        # O ar seca naturalmente quando esquenta (capacidade de reter água aumenta, umidade relativa cai)
        hum -= (state["temperatura"] * AIR_DRYING_FACTOR) * DT
        
        # A água que evaporou do solo vai para o ar! (Aumento da umidade)
        hum += water_evaporated * SOIL_TO_AIR_TRANSFER
        
        # Ruído natural
        hum += 0.05 * DT 
        hum += random.uniform(-0.1, 0.1) * DT
        
        state["umidade"] = round(max(10.0, min(100.0, hum)), 2)

        # --- PARTE C: SEGURANÇA (ALARMES) ---
        alarm_msg = ""
        if state["temperatura"] < 18: alarm_msg = "🚨 Temp Baixa!"
        elif state["temperatura"] > 35: alarm_msg = "🚨 Temp Alta!"
        elif state["soil_moisture"] < 20: alarm_msg = "🚨 Solo Seco!"
        
        state["alarm"] = alarm_msg

        # Registra no CSV e espera 1 segundo para o próximo ciclo
        append_history()
        time.sleep(DT)

# --- 11. ROTAS DO SITE (COMUNICAÇÃO COM O FRONTEND) ---

@app.route("/")
def home():
    """Carrega a página HTML principal."""
    return render_template("index.html")

@app.route("/dados")
def dados():
    """O JavaScript chama isso a cada 1s para pegar os números atualizados."""
    return jsonify(state)

@app.route("/comando", methods=["POST"])
def comando():
    """Recebe ordens do usuário (cliques nos botões)."""
    # Tenta ler o JSON enviado pelo navegador
    cmd = request.get_json(force=True, silent=True)
    if cmd:
        # Se o usuário clicar em um botão manual (Aquecedor/Ventilador/Bomba),
        # desligamos o modo automático para obedecer o usuário.
        if "aquecedor" in cmd:
            state["aquecedor"] = bool(cmd["aquecedor"])
            state["modo_auto"] = False
        if "ventilador" in cmd:
            state["ventilador"] = bool(cmd["ventilador"])
            state["modo_auto"] = False
        if "pump" in cmd:
            state["pump"] = bool(cmd["pump"])
            if not state["pump"]: state["pump_run_seconds"] = 0
            state["modo_auto"] = False
            
        # Se o usuário clicar na caixa "Modo Automático"
        if "modo_auto" in cmd:
            state["modo_auto"] = bool(cmd["modo_auto"])
            # Se ligou o automático, zeramos o PID para ele recomeçar limpo
            if state["modo_auto"]:
                global pid_integral, pid_last_error
                pid_integral = 0
                pid_last_error = 0

        # Botão para limpar a mensagem de erro
        if "reset_alarm" in cmd:
            state["alarm"] = ""
            
    return jsonify(state)

@app.route("/historico")
def historico():
    """Permite baixar o arquivo CSV gerado."""
    return send_file(HISTORY_FILE, as_attachment=True, download_name="historico.csv")

# --- 12. INICIALIZAÇÃO DO PROGRAMA ---
if __name__ == "__main__":
    # Cria e inicia a thread paralela que roda a função 'simular'
    # 'daemon=True' significa que se fecharmos o site, a simulação morre junto.
    thr = threading.Thread(target=simular, daemon=True)
    thr.start()
    
    # Inicia o servidor web Flask
    app.run(debug=True)