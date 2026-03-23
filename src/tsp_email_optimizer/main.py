from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from tsp_email_optimizer.adapters.email.imap_inbox import ImapInbox
from tsp_email_optimizer.adapters.email.smtp_sender import SmtpReplySender
from tsp_email_optimizer.adapters.llm.openai_extractor import OpenAiTripExtractor
from tsp_email_optimizer.adapters.persistence.csv_repository import CsvTripRepository
from tsp_email_optimizer.adapters.routing.ors_optimizer import OpenRouteServiceOptimizer
from tsp_email_optimizer.adapters.visualization.folium_route_map import FoliumRouteMapRenderer
from tsp_email_optimizer.config import AppConfig
from tsp_email_optimizer.services.pipeline import EmailOptimizationPipeline
from tsp_email_optimizer.services.reply_builder import ReplyBuilder

logger = logging.getLogger(__name__)


def build_pipeline(config: AppConfig) -> EmailOptimizationPipeline:
    inbox = ImapInbox(
        host=config.email_imap_host,
        port=config.email_imap_port,
        username=config.email_username,
        password=config.email_password,
        subject_contains=config.email_subject_contains,
        unread_scan_limit=config.email_unread_scan_limit,
    )
    sender = SmtpReplySender(
        host=config.email_smtp_host,
        port=config.email_smtp_port,
        username=config.email_username,
        password=config.email_password,
    )
    extractor = OpenAiTripExtractor(
        api_key=config.openai_api_key,
        model=config.openai_model,
        timezone=config.app_timezone,
        max_retries=config.max_extraction_retries,
    )
    optimizer = OpenRouteServiceOptimizer(
        api_key=config.openroute_api_key,
        profile=config.ors_profile,
        geocode_cache_path=str(Path(config.csv_output_path) / "geocode_cache.csv"),
    )
    map_renderer = (
        FoliumRouteMapRenderer(
            output_dir=config.map_output_path,
            api_key=config.openroute_api_key,
            profile=config.ors_profile,
        )
        if config.map_enabled
        else None
    )
    repository = CsvTripRepository(output_dir=config.csv_output_path)
    reply_builder = ReplyBuilder()
    return EmailOptimizationPipeline(
        inbox=inbox,
        sender=sender,
        extractor=extractor,
        optimizer=optimizer,
        repository=repository,
        reply_builder=reply_builder,
        map_renderer=map_renderer,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email-driven TSP optimizer.")
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=60,
        help="Polling interval when not using --once.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one unread email and exit.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger.info("Starting app log_level=%s once=%s", args.log_level, args.once)
    config = AppConfig.from_env()
    logger.info(
        "Loaded config imap_host=%r smtp_host=%r timezone=%r subject_filter=%r csv_output=%r map_enabled=%s map_output=%r",
        config.email_imap_host,
        config.email_smtp_host,
        config.app_timezone,
        config.email_subject_contains,
        config.csv_output_path,
        config.map_enabled,
        config.map_output_path,
    )
    logger.info("IMAP unread scan limit=%d", config.email_unread_scan_limit)
    pipeline = build_pipeline(config)

    if args.once:
        logger.info("Running single processing cycle (--once).")
        pipeline.process_next()
        logger.info("Single processing cycle completed.")
        return

    logger.info(
        "Running in polling mode interval_seconds=%d", args.poll_interval_seconds
    )
    while True:
        pipeline.process_next()
        time.sleep(args.poll_interval_seconds)


if __name__ == "__main__":
    main()

