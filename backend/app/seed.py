from sqlalchemy import select

from .db import SessionLocal
from .models import DayParity, PaymentProfile, Tariff, TariffWeekdayPrice, Unit


TARIFFS = [
    ("BANYA", "Пакет банный", "Дом + баня"),
    ("CHAN", "Пакет чанный", "Дом + чан"),
    ("BANYA_CHAN", "Пакет банно-чанный", "Дом + баня + чан"),
]

# Mon=0 ... Sun=6
WEEK_RULES = {
    "BANYA": {
        0: 10000, 1: 10000, 2: 10000, 3: 10000, 4: 13500, 5: 15000, 6: 10000,
    },
    "CHAN": {
        0: 11000, 1: 11000, 2: 11000, 3: 11000, 4: 13500, 5: 15000, 6: 11000,
    },
    "BANYA_CHAN": {
        0: 13000, 1: 13000, 2: 13000, 3: 13000, 4: 13500, 5: 15000, 6: 13000,
    },
}


async def seed_if_needed():
    async with SessionLocal() as session:
        # Домики
        r = await session.execute(select(Unit).limit(1))
        if r.scalar_one_or_none() is None:
            session.add_all([
                Unit(
                    id=1,
                    title='Дом "Треугольный"',
                    short_description="Семейный дом до 5 гостей.",
                    full_description=(
                        'Дом "Треугольный" подходит для семьи: до 2 взрослых и до 3 детей, '
                        "всего не более 5 гостей."
                    ),
                    max_total_guests=5,
                    max_adults=2,
                    max_children=3,
                    active=True,
                ),
                Unit(
                    id=2,
                    title='Мини-дом "Скворечник"',
                    short_description="Уютный домик только для двоих взрослых.",
                    full_description=(
                        'Мини-дом "Скворечник" подходит для спокойного отдыха вдвоём. '
                        "Размещение детей не предусмотрено."
                    ),
                    max_total_guests=2,
                    max_adults=2,
                    max_children=0,
                    active=True,
                ),
            ])
            await session.commit()

        # Тарифы
        for idx, (code, title, description) in enumerate(TARIFFS, start=1):
            r = await session.execute(select(Tariff).where(Tariff.code == code))
            if r.scalar_one_or_none() is None:
                session.add(Tariff(
                    id=idx,
                    code=code,
                    title=title,
                    description=description,
                    active=True,
                ))
        await session.commit()

        # Цены по дням недели
        for code, _, _ in TARIFFS:
            tariff_q = await session.execute(select(Tariff).where(Tariff.code == code))
            tariff = tariff_q.scalar_one()

            existing_q = await session.execute(
                select(TariffWeekdayPrice).where(TariffWeekdayPrice.tariff_id == tariff.id)
            )
            if existing_q.first() is None:
                for weekday in range(7):
                    session.add(TariffWeekdayPrice(
                        tariff_id=tariff.id,
                        weekday=weekday,
                        price_rub=WEEK_RULES[code][weekday],
                        is_available=True,
                    ))
                await session.commit()

        # Платежные профили — пока с заглушками
        payment_profiles = [
            (DayParity.EVEN, "Реквизиты для четного дня"),
            (DayParity.ODD, "Реквизиты для нечетного дня"),
        ]

        for parity, title in payment_profiles:
            q = await session.execute(
                select(PaymentProfile).where(PaymentProfile.day_parity == parity)
            )
            if q.scalar_one_or_none() is None:
                session.add(PaymentProfile(
                    title=title,
                    day_parity=parity,
                    recipient_name="ИП ТЕСТ",
                    bank_name="ТЕСТ БАНК",
                    personal_acc="00000000000000000000",
                    bic="000000000",
                    corr_acc="00000000000000000000",
                    active=True,
                ))
        await session.commit()