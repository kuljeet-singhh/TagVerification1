"""
Build a DEMO eval set from Wikipedia, so calibrate.py can be exercised before you
have your own creatives.

    ./.venv/bin/python fetch_demo_eval.py

Writes into eval/<tag>/{pos,neg}/, covering all 20 tags in packs.json at >=10
images per side. calibrate.py needs 8 (MIN_PER_CLASS) before it will call a tag
measured; the surplus is deliberate, because an article whose lead photo has been
removed upstream fails silently and would otherwise drop a tag under the floor.

This is a DEMO, not a substitute for real data. Wikipedia lead photos are clean,
centred product shots; DOOH creatives have text overlays, logos, models, and odd
crops. Thresholds calibrated on this set describe Wikipedia, not your inventory.
Replace these folders with real creatives you have actually run — the layout is the
same, so it is a re-run of calibrate.py and no code change.

What it does get right is the SHAPE of a good eval set. Every negative here is a
NEAR MISS chosen for the tag it sits under: another drink for `alcohol`, another
tub of powder for `protein_supplements`, ordinary clothing for `revealing_clothing`,
a nicotine patch for `vaping`. A folder of unrelated photos would prove nothing —
the model already scores a mountain at 0.03 for alcohol.

Image URLs are resolved through the action API in batches of 50 rather than one
REST summary call per article. At 468 images the per-article endpoint rate-limits
hard enough to report lead photos as missing, which silently shrinks the eval set.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

EVAL = Path("eval")
PACKS = Path("packs.json")
API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "dooh-tagcheck-eval/1.0 (contact: dev@localhost)"}

#: Titles per action-API request. 50 is the anonymous limit.
BATCH = 50

#: Ask for a thumbnail at this width, not `piprop=original`.
#:
#: Wikimedia rate-limits full-size originals from upload.wikimedia.org and says so in the
#: 429 body: "use thumbnail images in the sizes listed". Downloading originals here got
#: throttled after ~25 files. It also downloaded megabytes to throw them away, since every
#: image is immediately thumbnailed to 768 anyway. 800 keeps a little headroom above that.
THUMB_PX = 800

#: Pause between image downloads, and the base backoff after a 429.
#:
#: Wikimedia asks for a low request rate from anonymous clients and enforces it over a
#: window, so the cost of being impatient is not one slow file — it is the next several
#: minutes of every request failing. Measured the hard way: at a 1s gap the whole run
#: degraded to roughly one image per six minutes, all of it spent in backoff. 3s costs
#: about twenty minutes for a full 468-image fetch and does not trip the limiter at all.
DOWNLOAD_GAP_S = 3.0
RATE_LIMIT_BACKOFF_S = 20.0

#: Mirrors MIN_PER_CLASS in calibrate.py, which is the authority. Below this a tag is
#: scored but never marked calibrated, so it is the only count that matters here.
MIN_PER_CLASS = 8

DEMO: dict[str, dict[str, list[str]]] = {
    "alcohol": {
        # Negatives are all OTHER DRINKS, photographed the same way. A folder of unrelated
        # photos would prove nothing — near-misses are what measure a detector.
        "pos": [
            "Beer", "Wine", "Whisky", "Cocktail", "Champagne", "Vodka", "Tequila", "Rum", "Sake",
            "Gin", "Brandy", "Cider", "Liqueur", "Bourbon_whiskey"
        ],
        "neg": [
            "Orange_juice", "Coffee", "Milk", "Tea", "Lemonade", "Smoothie", "Coconut_water",
            "Soft_drink", "Energy_drink", "Iced_tea", "Milkshake", "Carbonated_water",
            "Hot_chocolate", "Kombucha"
        ],
    },
    "tobacco_smoking": {
        # Negatives are things held to the mouth or that smoulder: incense, a lollipop, a
        # sparkler. An inhaler is here on purpose and so is vaping hardware.
        "pos": [
            "Cigarette", "Cigar", "Hookah", "Tobacco_pipe", "Chewing_tobacco", "Beedi", "Snus",
            "Kretek", "Ashtray", "Tobacco", "Cigarillo"
        ],
        "neg": [
            "Incense", "Candle", "Chewing_gum", "Inhaler", "Lollipop", "Toothpick",
            "Drinking_straw", "Pencil", "Marker_pen", "Firecracker", "Sparkler", "Barbecue"
        ],
    },
    "vaping": {
        # Negatives are the neighbours that are NOT vaping: combustible tobacco, medical
        # inhalers, a nicotine patch, and pen/USB-shaped objects a vape is mistaken for.
        "pos": [
            "Electronic_cigarette", "Vaporizer_(inhalation_device)", "Juul",
            "Construction_of_electronic_cigarettes", "Electronic_cigarette_aerosol",
            "Nicotine_pouch", "Vape_shop", "Elf_Bar", "Puff_Bar", "Blu_(electronic_cigarette)",
            "Vype", "Cannabis_vaporizer"
        ],
        "neg": [
            "Cigarette", "Cigar", "Inhaler", "Nebulizer", "Fountain_pen", "USB_flash_drive",
            "Flashlight", "Humidifier", "Fog_machine", "Lipstick", "Bong", "Nicotine_patch"
        ],
    },
    "gym_fitness": {
        # Negatives are other bodies-in-rooms: massage, physiotherapy, ballet, a sauna.
        # Equipment-shaped furniture (office chair, rocking chair) is here too.
        "pos": [
            "Weight_training", "Dumbbell", "Barbell", "Treadmill", "Bench_press", "Gym",
            "Kettlebell", "Physical_fitness", "Elliptical_trainer", "Indoor_rower",
            "Squat_(exercise)", "Exercise_equipment"
        ],
        "neg": [
            "Massage", "Physical_therapy", "Ballet", "Sauna", "Spa", "Swimming_pool", "Golf",
            "Bowling", "Office_chair", "Playground", "Chess", "Rocking_chair"
        ],
    },
    "protein_supplements": {
        # The negatives are the whole point: every one is a TUB OR SCOOP OF POWDER that is
        # not protein. Baby formula reads as whey without them — see RULE 1 in packs.json.
        "pos": [
            "Whey_protein", "Protein_bar", "Dietary_supplement", "Creatine", "Meal_replacement",
            "Multivitamin", "Whey", "Soy_protein", "Casein", "Energy_bar"
        ],
        "neg": [
            "Infant_formula", "Powdered_milk", "Milkshake", "Flour", "Non-dairy_creamer",
            "Baking_powder", "Corn_starch", "Instant_coffee", "Sugar", "Laundry_detergent"
        ],
    },
    "gambling": {
        # Negatives are the other things in an arcade or a game cupboard: board games,
        # pinball, a jukebox, a vending machine, an ATM.
        "pos": [
            "Slot_machine", "Casino", "Roulette", "Poker", "Casino_token", "Playing_card", "Craps",
            "Blackjack", "Lottery", "Bingo_(British_version)", "Baccarat_(card_game)",
            "Sports_betting"
        ],
        "neg": [
            "Board_game", "Chess", "Amusement_arcade", "Vending_machine", "Pinball", "Jukebox",
            "Monopoly_(game)", "Dominoes", "Jigsaw_puzzle", "Automated_teller_machine",
            "Cash_register"
        ],
    },
    "junk_food": {
        # Negatives are food photographed identically but not junk — salad, sushi, soup,
        # fruit. Plated-food composition is what gets confused, not the ingredients.
        "pos": [
            "Hamburger", "Pizza", "French_fries", "Potato_chip", "Candy", "Doughnut", "Hot_dog",
            "Fried_chicken", "Nachos", "Onion_ring", "Cheeseburger", "Ice_cream"
        ],
        "neg": [
            "Salad", "Sushi", "Soup", "Fruit", "Vegetable", "Oatmeal", "Cooked_rice", "Yogurt",
            "Bread", "Apple", "Broccoli", "Lentil_soup"
        ],
    },
    "sugary_drinks": {
        # Negatives are unsweetened drinks in the same bottles and glasses: sparkling
        # water, black coffee, plain tea, buttermilk.
        "pos": [
            "Soft_drink", "Cola", "Energy_drink", "Lemonade", "Iced_tea", "Sports_drink",
            "Punch_(drink)", "Root_beer", "Ginger_ale", "Orange_soft_drink", "Bubble_tea",
            "Slush_(beverage)"
        ],
        "neg": [
            "Carbonated_water", "Mineral_water", "Coffee", "Tea", "Milk", "Coconut_water",
            "Bottled_water", "Kombucha", "Green_tea", "Herbal_tea", "Buttermilk", "Espresso"
        ],
    },
    "pharma_medicine": {
        # Negatives are small coloured things in blister-like packs — jelly beans, breath
        # mints, chewing gum — plus a multivitamin, which is the genuinely hard one.
        "pos": [
            "Tablet_(pharmacy)", "Capsule_(pharmacy)", "Pharmacy", "Syringe", "Blister_pack",
            "Medication", "Prescription_drug", "Stethoscope", "Ampoule", "Vaccine",
            "Pill_organizer", "Intravenous_therapy"
        ],
        "neg": [
            "Candy", "Chewing_gum", "Multivitamin", "Jelly_bean", "Sugar_substitute", "Cosmetics",
            "Lip_balm", "Soap", "Toothpaste", "Salt", "Confectionery", "Breath_mint"
        ],
    },
    "beauty_cosmetics": {
        # Negatives are other tubes, pots and liquids: toothpaste, shoe polish, paint, ink,
        # a crayon. Packaging shape is what the model latches onto.
        "pos": [
            "Cosmetics", "Lipstick", "Mascara", "Nail_polish", "Foundation_(cosmetics)",
            "Eye_shadow", "Skin_care", "Shampoo", "Beauty_salon", "Moisturizer"
        ],
        "neg": [
            "Toothpaste", "Soap", "Laundry_detergent", "Paint", "Ink", "Crayon", "Food_coloring",
            "Candle", "Marker_pen", "Adhesive", "Shoe_polish", "Hand_sanitizer"
        ],
    },
    "jewellery": {
        # Negatives are small shiny metal objects that are not jewellery: a coin, a zipper,
        # a screw, a washer, a bottle cap, a Christmas ornament.
        "pos": [
            "Jewellery", "Necklace", "Ring_(jewellery)", "Bracelet", "Earring", "Diamond", "Gold",
            "Pendant", "Brooch", "Engagement_ring", "Watch", "Gemstone"
        ],
        "neg": [
            "Keychain", "Coin", "Button", "Paper_clip", "Zipper", "Screw", "Bead",
            "Christmas_ornament", "Cutlery", "Door_handle", "Bottle_cap", "Washer_(hardware)"
        ],
    },
    "automotive": {
        # Negatives are other vehicles — bicycle, train, boat, tractor, forklift, stroller.
        # A car ad is a vehicle photographed well, so vehicles are the near-miss.
        "pos": [
            "Car", "Motorcycle", "Sport_utility_vehicle", "Car_dealership", "Tire", "Truck",
            "Sports_car", "Bus", "Scooter_(motorcycle)", "Automobile_repair_shop", "Steering_wheel",
            "Electric_car"
        ],
        "neg": [
            "Bicycle", "Train", "Airplane", "Boat", "Tractor", "Shopping_cart", "Wheelchair",
            "Baby_transport", "Skateboard", "Forklift", "Lawn_mower", "Golf_cart"
        ],
    },
    "real_estate": {
        # Negatives are other BUILDINGS: a hotel, an office, a warehouse, a school, a barn.
        # The tag is about property for sale, not about architecture.
        "pos": [
            "Apartment", "House", "Housing_estate", "Skyscraper", "Floor_plan", "Condominium",
            "Residential_area", "Villa", "Bungalow", "Terraced_house", "Suburb"
        ],
        "neg": [
            "Hotel", "Office", "Shopping_mall", "Factory", "Warehouse", "Hospital", "School",
            "Church_(building)", "Museum", "Library", "Tent", "Barn"
        ],
    },
    "fashion_apparel": {
        # Negatives are other textiles photographed like product shots — towels, bedding,
        # curtains, a carpet, a flag — plus a backpack against the handbags.
        "pos": [
            "Clothing", "Dress", "Shoe", "Handbag", "Fashion", "Jeans", "T-shirt",
            "Suit_(clothing)", "Sneakers", "Jacket", "Skirt", "Boutique"
        ],
        "neg": [
            "Towel", "Bedding", "Curtain", "Blanket", "Carpet", "Tablecloth", "Backpack",
            "Sleeping_bag", "Pillow", "Flag", "Upholstery", "Apron"
        ],
    },
    "restaurant_dining": {
        # Negatives are the other rooms food appears in: a kitchen, a supermarket, a bakery,
        # a picnic, a living room.
        "pos": [
            "Restaurant", "Chef", "Diner", "Cafeteria", "Waiting_staff", "Menu", "Dining_room",
            "Buffet", "Bistro", "Coffeehouse", "Table_setting"
        ],
        "neg": [
            "Kitchen", "Grocery_store", "Supermarket", "Picnic", "Vending_machine", "Living_room",
            "Classroom", "Bakery", "Butcher", "Farmers'_market", "Food_court"
        ],
    },
    "mobile_electronics": {
        # Negatives are other boxy household objects with panels and screens: a microwave, a
        # toaster, a clock, a picture frame, a calculator, a book.
        "pos": [
            "Smartphone", "Laptop", "Tablet_computer", "Television", "Headphones", "Smartwatch",
            "Personal_computer", "Camera", "Video_game_console", "Desktop_computer",
            "Computer_monitor", "Headphones#Earbuds"
        ],
        "neg": [
            "Book", "Notebook", "Calculator", "Radio_receiver", "Microwave_oven", "Refrigerator",
            "Washing_machine", "Toaster", "Clock", "Picture_frame", "Wallet", "Vacuum_cleaner"
        ],
    },
    "travel_tourism": {
        # Negatives are the anti-holiday: commuting, traffic, a parking lot, a bus stop, a
        # construction site. Both are places photographed wide.
        "pos": [
            "Tourism", "Resort", "Airplane", "Baggage", "Passport", "Hotel", "Cruise_ship",
            "Airport", "Backpacking_(travel)", "Eiffel_Tower", "Beach", "Tour_bus_service"
        ],
        "neg": [
            "Office", "Commuting", "Traffic_congestion", "Parking_lot", "Warehouse", "Bus_stop",
            "Construction_site", "Factory", "Supermarket", "Hospital", "Classroom", "Laundry"
        ],
    },
    "education": {
        # Negatives are other rooms full of seated people — a meeting, a courtroom, a
        # theatre, an auditorium, a waiting room.
        "pos": [
            "School", "Classroom", "Student", "University", "Teacher", "Lecture", "Library",
            "Graduation", "Textbook", "Blackboard", "Tutor", "Test_(assessment)"
        ],
        "neg": [
            "Office", "Meeting", "Courtroom", "Theatre", "Church_(building)", "Hospital", "Factory",
            "Auditorium", "Coworking_space", "Waiting_room"
        ],
    },
    "banking_finance": {
        # Negatives are the other places money changes hands: a cash register, a receipt, a
        # gift card, a wallet, a vending machine, a casino token.
        "pos": [
            "Bank", "Credit_card", "Automated_teller_machine", "Money", "Coin", "Insurance", "Loan",
            "Banknote", "Piggy_bank", "Cheque"
        ],
        "neg": [
            "Casino_token", "Shopping_mall", "Cash_register", "Receipt", "Calculator", "Wallet",
            "Vending_machine", "Post_office", "Library", "Gift_card", "Voucher"
        ],
    },
    "revealing_clothing": {
        # Negatives are ORDINARY CLOTHING — a dress, jeans, a coat, pyjamas. This tag is
        # compliance-sensitive, so a false positive here is expensive.
        "pos": [
            "Bikini", "Swimsuit", "Lingerie", "Undergarment", "Brassiere", "Sports_bra",
            "One-piece_swimsuit", "Nightgown", "Corset", "Camisole", "Boxer_shorts", "Bodysuit"
        ],
        "neg": [
            "Dress", "T-shirt", "Jeans", "Suit_(clothing)", "Coat", "Sweater", "Uniform", "Pajamas",
            "Tracksuit", "Raincoat", "Blouse", "Skirt"
        ],
    },
}

def _api(params: dict[str, str], attempts: int = 6) -> dict:
    """One action-API call, backing off on 429 rather than treating it as an answer."""
    query = urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(f"{API}?{query}", headers=UA)
            with urllib.request.urlopen(request, timeout=40) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            time.sleep(5.0 * (attempt + 1))
        except Exception:
            time.sleep(5.0 * (attempt + 1))
    raise RuntimeError(f"gave up after {attempts} attempts: {params.get('titles', '')[:60]}")


def resolve(articles: list[str]) -> dict[str, str]:
    """
    Map article title -> thumbnail URL, 50 titles per request.

    Titles come back normalised and redirect-followed, so each result is registered
    under every name that leads to it — otherwise a redirect looks like a missing image.
    """
    found: dict[str, str] = {}

    for start in range(0, len(articles), BATCH):
        payload = _api({
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "pageimages",
            "piprop": "thumbnail",
            "pithumbsize": str(THUMB_PX),
            "redirects": "1",
            "titles": "|".join(articles[start : start + BATCH]),
        })
        block = payload.get("query", {})

        home: dict[str, str] = {}
        for key in ("redirects", "normalized"):
            home.update({item["to"]: item["from"] for item in block.get(key, [])})

        for page in block.get("pages", []):
            url = (page.get("thumbnail") or {}).get("source")
            if not url:
                continue
            for name in _aliases(page["title"], home):
                found[name] = url

        print(f"\r  resolved {min(start + BATCH, len(articles))}/{len(articles)} titles",
              end="", flush=True)
        time.sleep(1.0)

    print()
    return found


def _aliases(title: str, home: dict[str, str]) -> set[str]:
    """Every name that leads to `title`: itself, its underscored form, and its redirects."""
    names = set()
    current: str | None = title
    while current and current not in names:
        names.add(current)
        names.add(current.replace(" ", "_"))
        current = home.get(current)
    return names


def download(url: str, destination: Path) -> bool:
    """
    Fetch one thumbnail and save it at 768px.

    A 429 gets a much longer backoff than an ordinary error. Wikimedia throttles by
    client over a window, so retrying a rate-limit after 1.5s just spends another
    token and fails again — which looked exactly like "this article has no image"
    and quietly shrank the eval set.
    """
    last: Exception | None = None
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            image.thumbnail((768, 768))
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, "JPEG", quality=90)
            return True
        except urllib.error.HTTPError as exc:
            last = exc
            time.sleep(RATE_LIMIT_BACKOFF_S * (attempt + 1) if exc.code == 429
                       else 1.5 * (attempt + 1))
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    print(f"\n    failed {destination.name}: {last}")
    return False


def check_slugs() -> None:
    """
    Refuse a slug that is not in packs.json.

    A typo used to create an eval/<typo>/ folder that nothing complained about until
    calibrate.py exited 2 on the whole run, long after the images had been downloaded.
    """
    known = {tag["slug"] for tag in json.loads(PACKS.read_text())["tags"]}
    unknown = sorted(set(DEMO) - known)
    if unknown:
        raise SystemExit(
            f"not in packs.json: {', '.join(unknown)}\n"
            f"known: {', '.join(sorted(known))}"
        )

    missing = sorted(known - set(DEMO))
    if missing:
        print(f"note: no demo images defined for {', '.join(missing)}\n")


def main() -> None:
    check_slugs()

    wanted: list[tuple[str, str, str, Path]] = []
    for slug, buckets in DEMO.items():
        for label, articles in buckets.items():
            for article in articles:
                wanted.append((slug, label, article, EVAL / slug / label / f"{article}.jpg"))

    todo = [item for item in wanted if not item[3].exists()]
    print(f"{len(wanted)} images wanted, {len(wanted) - len(todo)} already on disk\n")

    urls = resolve(sorted({article for _, _, article, _ in todo})) if todo else {}

    fetched = 0
    no_image: list[str] = []
    for index, (_slug, _label, article, destination) in enumerate(todo, 1):
        url = urls.get(article) or urls.get(article.replace("_", " "))
        if not url:
            no_image.append(article)
            continue
        if download(url, destination):
            fetched += 1
        print(f"\r  fetched {fetched}/{index} of {len(todo)}", end="", flush=True)
        time.sleep(DOWNLOAD_GAP_S)
    print()

    summarise(no_image)


def summarise(no_image: list[str]) -> None:
    """
    Per-tag counts, and a loud line for anything under MIN_PER_CLASS.

    The script used to print failures inline and finish with a total, so a tag that
    ended up with six usable images looked exactly like one that ended up with twelve.
    That difference decides whether calibrate.py will call the tag measured at all.
    """
    print(f"\n{'tag':24}{'pos':>5}{'neg':>5}")
    short: list[str] = []
    for slug in DEMO:
        counts = {}
        for label in ("pos", "neg"):
            folder = EVAL / slug / label
            counts[label] = len(list(folder.glob("*.jpg"))) if folder.is_dir() else 0
        flag = "" if min(counts.values()) >= MIN_PER_CLASS else f"  <-- under {MIN_PER_CLASS}"
        if flag:
            short.append(slug)
        print(f"{slug:24}{counts['pos']:>5}{counts['neg']:>5}{flag}")

    if no_image:
        print(f"\n{len(no_image)} article(s) with no lead image upstream:")
        print("  " + ", ".join(sorted(set(no_image))))

    if short:
        print(
            f"\nSHORT: {', '.join(short)}"
            f"\ncalibrate.py will score these but will NOT mark them calibrated."
            f"\nAdd more articles to DEMO, or drop real creatives into eval/<tag>/{{pos,neg}}/."
        )
    else:
        print(f"\nEvery tag has at least {MIN_PER_CLASS} images per side.")

    print("\nNow run:\n    ./.venv/bin/python calibrate.py")
    print(
        "\nRemember: these are Wikipedia product shots, not DOOH creatives. Replace\n"
        "them with real ones before trusting the thresholds."
    )


if __name__ == "__main__":
    main()
