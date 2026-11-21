.PHONY: install test lint doctor step chat-step run sync

install:
	pip install -e ".[test]"

test:
	pytest -v
	python -m unittest discover -s tests -p "test_stdlib_*.py" -v

lint:
	python -m compileall -q ai bot core database filters tools web scripts cli.py main.py config.py

doctor:
	heart3 doctor

step:
	heart3 step

chat-step:
	heart3 chat-step

run:
	heart3 run
