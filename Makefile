.PHONY: install dev demo test lint init-db clean docker-up

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev]'

demo:
	python -m qp demo

test:
	python -m pytest -q

lint:
	python -m ruff check src tests

init-db:
	python -m qp init-db

docker-up:
	docker compose up -d postgres clickhouse

clean:
	rm -rf data reports .pytest_cache .ruff_cache
