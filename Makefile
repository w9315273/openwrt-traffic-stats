BUILD_DIR ?= .local/build
PYTHON ?= python3
TEST_TMP_DIR := $(abspath .local/tmp)
PYTHON_SOURCES := $(wildcard src/traffic_stats/*.py)

.PHONY: all clean test test-backend test-frontend
all:
	mkdir -p $(BUILD_DIR)/traffic_stats
	cp $(PYTHON_SOURCES) $(BUILD_DIR)/traffic_stats/
	cp src/traffic-statsd src/traffic-stats-rpc $(BUILD_DIR)/
	chmod 755 $(BUILD_DIR)/traffic-statsd $(BUILD_DIR)/traffic-stats-rpc

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD_DIR)

$(TEST_TMP_DIR):
	mkdir -p $@

test: test-backend test-frontend

# 浏览器沿用系统临时目录, 避免深层检出路径超过 Unix socket 路径上限.
test-backend: export TMPDIR := $(TEST_TMP_DIR)
test-backend test-frontend: export PYTHONDONTWRITEBYTECODE := 1

test-backend: all | $(TEST_TMP_DIR)
	@$(PYTHON) tests/run-python.py --label '后端' discover tests/backend --pattern 'test_python_*.py'
	@$(PYTHON) tests/run-python.py --label '命令行' file tests/backend/test_cli.py $(BUILD_DIR)/traffic-statsd
	@$(PYTHON) tests/run-python.py --label '采集器' file tests/backend/test_collector.py $(BUILD_DIR)/traffic-statsd
	@$(PYTHON) tests/run-python.py --label 'RPC ' file tests/backend/test_rpc.py $(BUILD_DIR)/traffic-stats-rpc

test-frontend: | $(TEST_TMP_DIR)
	@$(PYTHON) tests/run-python.py --label '浏览器' discover tests/frontend
