# Route Optimization TSP (Email-Driven)

Python app that:
- reads unread emails from IMAP
- extracts date/time/addresses with `gpt-4o-mini`
- writes extracted data to CSV
- optimizes stop order with openrouteservice
- replies by email with the optimized route

## Project Structure

- `src/tsp_email_optimizer/domain`: domain models and interfaces
- `src/tsp_email_optimizer/services`: application orchestration
- `src/tsp_email_optimizer/adapters`: external integrations (email, OpenAI, CSV, ORS)
- `INTEGRATION_STEPS.md`: integration and architecture notes

## Setup

1. Create virtual environment and install dependencies:
   - `python -m venv .venv`
   - `source .venv/bin/activate`
   - `pip install -r requirements.txt`
2. Copy and fill environment file:
   - `cp .env.example .env`
3. Export environment variables from `.env` (or use your preferred loader).

## Run

Process one unread email and exit:

- `PYTHONPATH=src python -m tsp_email_optimizer.main --once`

Poll continuously every 60 seconds:

- `PYTHONPATH=src python -m tsp_email_optimizer.main --poll-interval-seconds 60`

## SOLID-Oriented Design Notes

- **Single Responsibility**: each adapter handles one infrastructure concern.
- **Open/Closed**: swap providers (other LLMs, routing APIs) by implementing domain protocols.
- **Liskov Substitution**: services depend on behavior contracts, not concrete classes.
- **Interface Segregation**: inbox, sender, extractor, optimizer, repository interfaces are small and focused.
- **Dependency Inversion**: `EmailOptimizationPipeline` depends on abstractions from `domain/ports.py`.

