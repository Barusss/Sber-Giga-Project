.PHONY: up down logs test
up:
	docker compose up --build
down:
	docker compose down
logs:
	docker compose logs -f
test:
	cd backend && pytest -q
