"""ucbatch — motor de batch backtesting dirigido por `Backtesting_use_cases_template.xlsx`.

Data seed (globales) + Backtesting scenarios (condiciones por escenario) son la ÚNICA fuente de
configuración. Reemplaza a `batch_runner`. Módulos:
  reader   — lee el Excel (Data seed + scenarios)            [I/O aislado]
  scenario — Seed + Scenario → kwargs de run_one + colectivo [parser de dominio]
  runner   — orquesta escenario × día × ticker (multiproceso) [usa run_one + apply_collective_exit]
  metrics  — métricas de ROI por posición
  report   — genera el workbook de salida
  progress — progreso + notificación
La lógica de negocio no depende de la UI; el parsing y el reporte están aislados del engine.
"""
