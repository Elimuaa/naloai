"""
Stripe payment integration for Nalo.Ai Pro subscriptions.
Handles checkout sessions, webhooks, and subscription management.
"""

import os
import logging
import stripe
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from database import get_db, User, AsyncSession
from auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stripe", tags=["stripe"])

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def _get_stripe_config():
    return {
        "secret_key": os.getenv("STRIPE_SECRET_KEY", ""),
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
        "price_id": os.getenv("STRIPE_PRICE_ID", ""),
    }


@router.get("/config")
async def get_stripe_config():
    """Return publishable key for frontend."""
    cfg = _get_stripe_config()
    return {
        "publishable_key": cfg["publishable_key"],
        "configured": bool(cfg["secret_key"] and cfg["price_id"]),
    }


@router.post("/create-checkout")
async def create_checkout_session(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Create a Stripe Checkout Session for Pro subscription."""
    cfg = _get_stripe_config()
    if not cfg["secret_key"] or not cfg["price_id"]:
        raise HTTPException(500, "Stripe not configured")

    stripe.api_key = cfg["secret_key"]

    # Check if user already has a Stripe customer ID
    customer_id = getattr(current_user, 'stripe_customer_id', None)

    try:
        # Create or reuse customer
        if not customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                metadata={"user_id": current_user.id},
            )
            customer_id = customer.id
            # Save customer ID to user record
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(User).where(User.id == current_user.id).values(
                        stripe_customer_id=customer_id
                    )
                )
                await db.commit()

        # Determine base URL from request origin
        origin = request.headers.get("origin", "")
        base_url = origin or os.getenv("BASE_URL", "")

        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{
                "price": cfg["price_id"],
                "quantity": 1,
            }],
            mode="subscription",
            success_url=f"{base_url}/dashboard?premium=success",
            cancel_url=f"{base_url}/dashboard?premium=cancelled",
            metadata={
                "user_id": current_user.id,
            },
        )

        return {"checkout_url": session.url, "session_id": session.id}

    except stripe.StripeError as e:
        logger.error(f"Stripe checkout error: {e}")
        raise HTTPException(400, f"Stripe error: {str(e)}")


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events for subscription lifecycle."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    stripe.api_key = _get_stripe_config()["secret_key"]

    # If webhook secret is set, verify signature
    if webhook_secret:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except (ValueError, stripe.SignatureVerificationError) as e:
            logger.warning(f"Webhook signature failed: {e}")
            raise HTTPException(400, "Invalid signature")
    else:
        # Without webhook secret, parse event directly (dev mode)
        import json
        payload_str = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        event = stripe.Event.construct_from(json.loads(payload_str), stripe.api_key)

    event_type = event["type"]
    data = event["data"]["object"]
    logger.info(f"Stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_cancelled(data)
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(data)
    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(data)

    return {"received": True}


async def _handle_checkout_completed(session):
    """Activate premium after successful checkout."""
    user_id = session.get("metadata", {}).get("user_id")
    subscription_id = session.get("subscription")
    customer_id = session.get("customer")

    if not user_id:
        logger.warning("Checkout completed but no user_id in metadata")
        return

    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(
                is_premium=True,
                premium_since=datetime.now(timezone.utc),
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
            )
        )
        await db.commit()

    logger.info(f"Premium activated for user {user_id} via Stripe checkout")

    # Notify via WebSocket
    from ws_manager import ws_manager
    await ws_manager.send_to_user(user_id, {
        "type": "premium_activated",
        "message": "Nalo.Ai Pro is now active! AI auto-calibration will start with your next trade.",
    })


async def _handle_subscription_cancelled(subscription):
    """Deactivate premium when subscription is cancelled."""
    customer_id = subscription.get("customer")
    if not customer_id:
        return

    from database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            await db.execute(
                update(User).where(User.id == user.id).values(
                    is_premium=False,
                    stripe_subscription_id=None,
                )
            )
            await db.commit()
            logger.info(f"Premium deactivated for user {user.id} — subscription cancelled")


async def _handle_subscription_updated(subscription):
    """Handle subscription status changes (e.g., past_due)."""
    status = subscription.get("status")
    customer_id = subscription.get("customer")

    if status in ("past_due", "unpaid", "canceled"):
        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.stripe_customer_id == customer_id)
            )
            user = result.scalar_one_or_none()
            if user:
                await db.execute(
                    update(User).where(User.id == user.id).values(is_premium=False)
                )
                await db.commit()
                logger.info(f"Premium paused for user {user.id} — status: {status}")


async def _handle_payment_failed(invoice):
    """Notify user of failed payment."""
    customer_id = invoice.get("customer")
    if not customer_id:
        return

    from database import AsyncSessionLocal
    from ws_manager import ws_manager
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            await ws_manager.send_to_user(user.id, {
                "type": "payment_failed",
                "message": "Your Pro subscription payment failed. Please update your payment method to keep AI auto-calibration active.",
            })


@router.post("/cancel")
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel the user's Pro subscription."""
    sub_id = getattr(current_user, 'stripe_subscription_id', None)
    if not sub_id:
        # No Stripe subscription — just deactivate locally
        await db.execute(
            update(User).where(User.id == current_user.id).values(is_premium=False)
        )
        await db.commit()
        return {"message": "Premium deactivated", "is_premium": False}

    stripe.api_key = _get_stripe_config()["secret_key"]

    try:
        # Cancel at end of billing period (user keeps access until then)
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        return {
            "message": "Subscription will cancel at end of billing period. You'll keep Pro access until then.",
            "is_premium": True,
            "cancelling": True,
        }
    except stripe.StripeError as e:
        logger.error(f"Stripe cancel error: {e}")
        # Fallback: deactivate locally
        await db.execute(
            update(User).where(User.id == current_user.id).values(
                is_premium=False, stripe_subscription_id=None
            )
        )
        await db.commit()
        return {"message": "Premium deactivated", "is_premium": False}


@router.get("/portal")
async def create_portal_session(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Create a Stripe Customer Portal session for managing subscription."""
    customer_id = getattr(current_user, 'stripe_customer_id', None)
    if not customer_id:
        raise HTTPException(400, "No active subscription")

    stripe.api_key = _get_stripe_config()["secret_key"]
    origin = request.headers.get("origin", "")
    base_url = origin or os.getenv("BASE_URL", "")

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base_url}/dashboard",
        )
        return {"portal_url": session.url}
    except stripe.StripeError as e:
        raise HTTPException(400, f"Portal error: {str(e)}")
