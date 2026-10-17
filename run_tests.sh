#!/usr/bin/env bash
# Ejecuta todos los tests (no hacen peticiones reales: usan un exchange simulado)
set -e
for f in tests/test_*.py; do echo "== $f"; python "$f"; done
echo "TODOS LOS TESTS OK"
