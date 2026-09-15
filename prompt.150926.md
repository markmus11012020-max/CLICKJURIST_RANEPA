# Technical Specification (TS): ClickJurist Production Evolution

## 1. Project Overview & Target Audience
**ClickJurist** is an AI-powered LegalTech assistant delivering structured legal consultations, actionable checklists, and automated PDF document generation based on Russian Federation legislation.

### Target Slogans & Audience Focus:
*   **For Individuals:** *"ClickJurist: Понятные ответы на сложные правовые вопросы. Быстро, анонимно и со ссылками на законы РФ."*
*   **For Professionals (Lawyers/Attorneys):** *"Интеллектуальный ассистент адвоката. Делегируйте рутину ИИ: мгновенный подбор практики, фактчекинг и сборка черновиков документов."*

---

## 2. Core Architectural Upgrades & 152-FZ Compliance
To comply with Russian Federal Law **"On Personal Data" (152-FZ)**, the backend pipeline isolates user personal identifiable information (PII) using a strict two-stage data contour.

### Two-Stage Secure AI Pipeline
1.  **Stage 1: PII Masking & Data Sanitization Contour (Russian LLM)**
    *   **Objective:** Prevent raw user personal data from leaking to international AI endpoints.
    *   **Execution:** Before routing requests to secondary models, a Russian-hosted LLM (YandexGPT or a local instance via Ollama) processes the raw text. It identifies and replaces all PII (Full Names, specific addresses, passport details, phone numbers) with standardized placeholders (e.g., `[NAME_1]`, `[ADDRESS_1]`) and generates an anonymous semantic summary of the legal issue.
2.  **Stage 2: Advanced Analysis & Web Fact-Checking Contour (Gemini)**
    *   **Objective:** Generate a precise legal response using external web tools without violating compliance.
    *   **Execution:** The advanced model (Gemini 2.5 Flash via AITunnel) receives the **anonymized summary**. It leverages web search tools to cross-verify current laws and Supreme Court rulings across **no less than two and no more than three independent web sources**.
    *   **Strict Output Rules:** If the sources match and verify the data, the final output includes active, valid links to legislation. If the web data is conflicting, the model returns a clear logical breakdown *without* potentially hallucinated links. Explicit citations of Russian legal codes remain mandatory for every response.

### Security & Cloud Infrastructure Stack (Yandex Cloud)
*   **Data Isolation:** Migrate deployment infrastructure from Vercel/Amvera to an isolated **Yandex Virtual Private Cloud (VPC)**.
*   **Secrets Management:** All API keys, database credentials, and environments (`.env`) must be managed and encrypted via **Yandex Key Management Service (KMS)**.
*   **Zero-Storage Logging:** Audit logs are routed via **Yandex Cloud Logging**. Raw personal data is strictly forbidden from entering logs. Only anonymized UUIDs of sessions are recorded.

---

## 3. Cross-Provider Failover Mechanism (Resilience)
To mitigate external API crashes (such as AITunnel connection timeouts or token exhaustion), the application implements an asynchronous **Failover Orchestrator** in `backend/services/llm_chain.py`.

### Failover Logic Architecture
*   **Default State:** The main pipeline executes tasks using AITunnel (Gemini/Minimax).
*   **Fallback Trigger:** Any network anomaly, timeout, or HTTP 5xx error from AITunnel triggers a `try-except` block.
*   **Resilience Routing:** The system catches the exception, logs an error (`[ERROR] AITunnel Down. Switching context to YandexGPT...`), and routes the full legal analysis load to the Yandex Cloud API.
*   **Bidirectional Capability:** The architecture allows seamless flipping of roles—if YandexGPT is configured as primary and fails, the system routes traffic back to AITunnel.

---

## 4. Local Development & Robokassa Integration
Before pushing code into production, the financial loop must be fully implemented and tested locally.

### Monetization Logic (Free Tier Guardrail)
*   **First Request Free:** A local lightweight database (SQLite for development / PostgreSQL for production) tracks incoming requests using hashed user IP and browser fingerprints. 
*   **Free State Check:** If a user session is flagged as `is_free=True`, the request bypasses the payment wall. 
*   **Payment Trigger:** Subsequent requests block generation, log a `402 Payment Required` status code, and generate a dynamic check invoice url via the payment system.

### Robokassa Integration Workflow
*   **Sandbox Configuration:** Implement the Robokassa SDK in `backend/config.py` using `test=1` parameters.
*   **State Machine:**
    1. User initiates a paid document generation.
    2. Backend creates an invoice and returns a secure payment link.
    3. Robokassa coordinates callback webhooks (`/api/payment/success` and `/api/payment/fail`).
    4. Upon successful webhook verification, the backend unlocks the asynchronous FastAPI pipeline to generate the requested PDF.

---

## 5. On-Site Documentation Requirements
The frontend (`frontend/index.html`) must be expanded with accessible, professional legal documentation tabs:
1.  **Terms of Service & Offer Agreement:** Clear definition of the free trial limits, payment terms via Robokassa, and subscription terms if applicable.
2.  **Privacy Policy (152-FZ Aligned):** Transparency note detailing that the system does not process or retain unmasked personal data on servers, explaining the instant client-side rendering.
3.  **AI Performance Disclaimer:** A permanent visibility banner stating: *"Generative AI can make mistakes. For citizens, ClickJurist is an initial action map; for professional lawyers, it is an efficient automated tool to optimize manual routine operations."*

---

## 6. Deployment Strategy (Yandex Cloud Production)
*   **Containerization:** Containerize the entire Python 3.11 FastAPI app alongside its static vanilla frontend assets using a clean, multi-stage `Dockerfile`.
*   **Production Host:** Deploy via **Yandex Compute Cloud** using Docker Compose or utilize serverless infrastructure via **Yandex Serverless Containers**.
*   **Network & Routing:** Route the production domain (`clickjurist.ru`) using **Yandex Cloud DNS**. Bind it with automated SSL certification management via **Yandex Certificate Manager** to maintain standard HTTPS communication encryption.
