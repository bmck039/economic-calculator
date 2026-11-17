"""Chart style shared by every tab."""

from __future__ import annotations

# Validated categorical palette (fixed order, never cycled) - dataviz reference.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, MUTED = "#52514e", "#8a8984"

GROUPS = {  # expense column -> display group (8 groups = 8 palette slots)
    "rent": "Housing",
    "mortgage_payment": "Housing",
    "owner_costs": "Housing",
    "budget:groceries": "Food",
    "budget:restaurants_hotels": "Food",
    "budget:energy_home": "Utilities & home",
    "budget:water_refuse": "Utilities & home",
    "budget:home_maintenance": "Utilities & home",
    "budget:furnishings_household": "Utilities & home",
    "budget:phone_internet": "Utilities & home",
    "budget:household_operations": "Utilities & home",
    "budget:utilities_phone": "Utilities & home",
    "budget:council_tax": "Utilities & home",
    "budget:vehicle_purchase": "Transport",
    "budget:fuel_vehicle": "Transport",
    "budget:vehicle_maintenance": "Transport",
    "budget:vehicle_other": "Transport",
    "budget:transport_services": "Transport",
    "budget:insurance_vehicle": "Transport",
    "budget:vehicle_all": "Transport",
    "budget:transport_all": "Transport",
    "budget:vehicle_running": "Transport",
    "budget:health_out_of_pocket": "Health & insurance",
    "budget:insurance_health": "Health & insurance",
    "budget:health_insurance_mandatory": "Health & insurance",
    "budget:insurance_home": "Health & insurance",
    "budget:insurance_other": "Health & insurance",
    "children": "Children",
    "childcare": "Children",
    "university": "Children",
    "long_term_care": "Health & insurance",
    "cars": "Transport",
    "public_transport": "Transport",
    "health": "Health & insurance",
    "mortgage_insurance": "Housing",
    "mortgage_interest_relief": "Housing",
    "wealth_tax": "Leisure & other",
    "student_loan": "Emergencies & one-offs",
    "emergencies": "Emergencies & one-offs",
    "oneoff:wedding": "Emergencies & one-offs",
    "oneoff:house_purchase": "Emergencies & one-offs",
}
GROUP_ORDER = [
    "Housing",
    "Food",
    "Transport",
    "Utilities & home",
    "Health & insurance",
    "Children",
    "Leisure & other",
    "Emergencies & one-offs",
]


def group_of(col: str) -> str:
    return GROUPS.get(col, "Leisure & other")


def base_layout(fig, title, ytitle):
    fig.update_layout(
        title=dict(text=title, x=0, font=dict(size=16)),
        height=430,
        margin=dict(l=10, r=10, t=50, b=10),
        hovermode="x unified",
        legend=dict(orientation="h", y=-0.18, x=0),
        yaxis=dict(
            title=ytitle, gridcolor="rgba(128,128,128,0.15)", zerolinecolor="rgba(128,128,128,0.4)"
        ),
        xaxis=dict(title="Partner 1 age", gridcolor="rgba(0,0,0,0)"),
        bargap=0.15,
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig


def add_event_markers(fig, events, y=None):
    for e in events:
        fig.add_vline(x=e["age"], line=dict(color=MUTED, width=1, dash="dot"))
        fig.add_annotation(
            x=e["age"],
            y=1.02,
            yref="paper",
            text=e["event"],
            showarrow=False,
            font=dict(size=11, color=INK),
            textangle=-30,
            xanchor="left",
        )
