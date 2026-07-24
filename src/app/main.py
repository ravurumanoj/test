
"""FastAPI application entry point for the relationship manager POC."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agents.crm_agent import CrmAgent
from app.agents.portfolio_agent import PortfolioAgent
from app.agents.relationship_manager import RelationshipManagerOrchestrator
from app.api.routes import create_router
from app.api.portfolio_routes import router as portfolio_router
from app.api.crm_routes import router as crm_router
from app.errors import AppError
from app.logging_config import configure_logging
from app.settings import Settings
from app.services.unique_client import UniqueAIClient
from app.services.unique_toolkit import UniqueToolkit

configure_logging()
logger = logging.getLogger(__name__)
settings = Settings.from_env()

unique_client = UniqueAIClient(settings=settings)
unique_toolkit = UniqueToolkit(client=unique_client)
portfolio_agent = PortfolioAgent(unique_toolkit=unique_toolkit)
crm_agent = CrmAgent(unique_toolkit=unique_toolkit)
orchestrator = RelationshipManagerOrchestrator(
	portfolio_agent=portfolio_agent,
	crm_agent=crm_agent,
	unique_toolkit=unique_toolkit,
	settings=settings,
)

app = FastAPI(title=settings.app_name)
app.include_router(create_router(orchestrator=orchestrator, settings=settings))
app.include_router(portfolio_router)
app.include_router(crm_router)


@app.exception_handler(AppError)
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
	"""Convert known application exceptions into safe API responses."""
	logger.error("Application error: %s", exc.message, extra={"details": exc.details})
	return JSONResponse(
		status_code=exc.status_code,
		content={"error": exc.error_code, "message": exc.message, "details": exc.details},
	)


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
	"""Convert unexpected exceptions into a generic API error response."""
	logger.exception("Unexpected server error")
	return JSONResponse(
		status_code=500,
		content={
			"error": "UNEXPECTED_ERROR",
			"message": "An unexpected error occurred.",
			"details": {"exception_type": exc.__class__.__name__},
		},
	)


