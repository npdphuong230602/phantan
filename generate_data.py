# generate_data.py
# ═══════════════════════════════════════════════════════════
# EXECUTION ORDER: Run this script FIRST
# Terminal: python generate_data.py
# ═══════════════════════════════════════════════════════════

import os
import sqlite3
import random

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# ---------------------------------------------------------------------------
# Step 1: Try downloading UCI Online Retail dataset via ucimlrepo
# If download fails (no internet), fall back to synthetic data
# Source: Chen, D. (2015). Online Retail [Dataset]. UCI ML Repository.
#         https://doi.org/10.24432/C5BW33
# License: CC BY 4.0
# ---------------------------------------------------------------------------


def download_uci_data():
    """Download the Online Retail dataset from UCI ML Repository.

    Returns a DataFrame with columns including Description and UnitPrice.
    The UCI API splits columns: StockCode/InvoiceNo go to data.ids,
    while Description/UnitPrice/etc. go to data.features.
    We merge them to get StockCode for product grouping.

    Returns None if download fails.
    """
    try:
        from ucimlrepo import fetch_ucirepo
        online_retail = fetch_ucirepo(id=352)
        df = online_retail.data.features

        # UCI API puts identifier columns (StockCode, InvoiceNo) in data.ids
        # We need to merge them back to get StockCode for product grouping
        if online_retail.data.ids is not None:
            ids_df = online_retail.data.ids
            # Concatenate along columns (same row index)
            df = pd.concat([ids_df, df], axis=1)

        return df
    except Exception as e:
        print(f"UCI dataset unavailable ({e}), using synthetic data")
        return None


def generate_synthetic_data():
    """Generate 1000 synthetic products as fallback.

    Creates realistic product names and prices when UCI download fails.
    Each product gets a sequential ItemID, a descriptive name, a category
    (Electronics / Clothing / Food), random stock level, and random price.
    """
    # Generate realistic product names for each category
    electronics_names = [f"Electronic Device {i}" for i in range(1, 334)]
    clothing_names = [f"Clothing Item {i}" for i in range(1, 334)]
    food_names = [f"Food Product {i}" for i in range(1, 335)]
    all_names = electronics_names + clothing_names + food_names

    data = []
    for i in range(1000):
        data.append({
            'ItemID': i + 1,
            'ItemName': all_names[i],
            'Category': 'Electronics' if i < 333 else ('Clothing' if i < 666 else 'Food'),
            'Stock': random.randint(10, 100),
            'Price': round(random.uniform(0.5, 50.0), 2),
            'Version': 0
        })
    return pd.DataFrame(data)


def process_uci_data(df):
    """Process raw UCI data into our inventory schema.

    Steps:
        - Determine product grouping key (StockCode if available, else Description)
        - Group by product key, take mean UnitPrice per product
        - Drop nulls and duplicates
        - Take first 1,000 unique products
        - Create ItemID 1-1000, assign categories by range
    """
    # Determine which column to use as product identifier
    # StockCode is preferred but may be in data.ids (merged in download_uci_data)
    # If not available, fall back to Description
    if 'StockCode' in df.columns:
        group_key = 'StockCode'
    elif 'Description' in df.columns:
        group_key = 'Description'
    else:
        print("WARNING: No suitable product grouping column found")
        return generate_synthetic_data()

    print(f"  Using '{group_key}' as product grouping key")

    # Group by product key, get first Description and mean UnitPrice
    agg_dict = {'UnitPrice': 'mean'}
    if 'Description' in df.columns and group_key != 'Description':
        agg_dict['Description'] = 'first'

    products = df.groupby(group_key).agg(agg_dict).reset_index()

    # If we grouped by Description, the product name is the group key itself
    if group_key == 'Description':
        products['ProductName'] = products['Description']
    else:
        products['ProductName'] = products.get('Description', products[group_key])

    # Drop nulls and rows with invalid prices
    products = products.dropna(subset=['ProductName', 'UnitPrice'])
    products = products[products['UnitPrice'] > 0]
    products = products.drop_duplicates(subset=[group_key])

    # Take first 1000
    products = products.head(1000).reset_index(drop=True)

    # If we got fewer than 1000, pad with synthetic
    if len(products) < 1000:
        print(f"Only {len(products)} products from UCI, padding to 1000")
        for i in range(len(products), 1000):
            products = pd.concat([products, pd.DataFrame([{
                group_key: f'SYNTH{i}',
                'ProductName': f'Synthetic Product {i}',
                'UnitPrice': round(random.uniform(0.5, 50.0), 2)
            }])], ignore_index=True)

    # Build inventory DataFrame matching our DB schema
    inventory = pd.DataFrame({
        'ItemID': range(1, 1001),
        'ItemName': products['ProductName'].values[:1000],
        'Category': [
            'Electronics' if i < 333 else ('Clothing' if i < 666 else 'Food')
            for i in range(1000)
        ],
        'Stock': [random.randint(10, 100) for _ in range(1000)],
        'Price': [round(float(p), 2) for p in products['UnitPrice'].values[:1000]],
        'Version': [0] * 1000
    })
    return inventory


def create_site_database(db_path, items_df):
    """Create a SQLite database for one site with the given items.

    Args:
        db_path: Absolute path to the SQLite database file.
        items_df: DataFrame containing inventory rows for this site.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            ItemID INTEGER PRIMARY KEY,
            ItemName TEXT NOT NULL,
            Category TEXT NOT NULL,
            Stock INTEGER NOT NULL,
            Price REAL NOT NULL,
            Version INTEGER DEFAULT 0
        )
    ''')

    # Clear existing data so the script is idempotent
    cursor.execute('DELETE FROM inventory')

    # Insert items row by row
    for _, row in items_df.iterrows():
        cursor.execute(
            'INSERT INTO inventory (ItemID, ItemName, Category, Stock, Price, Version) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (int(row['ItemID']), str(row['ItemName']), str(row['Category']),
             int(row['Stock']), float(row['Price']), int(row['Version']))
        )

    conn.commit()
    conn.close()


def main():
    """Main entry point: generate data and split across 3 sites."""
    # Ensure data directory exists
    os.makedirs(DATA_DIR, exist_ok=True)

    # Step 1: Try UCI download
    print("Attempting to download UCI Online Retail dataset...")
    raw_df = download_uci_data()

    # Step 2-3: Process or generate synthetic
    if raw_df is not None:
        print("UCI dataset downloaded successfully. Processing...")
        inventory = process_uci_data(raw_df)
    else:
        print("WARNING: UCI dataset unavailable, using synthetic data")
        inventory = generate_synthetic_data()

    # Step 4: Split and insert into SQLite per site
    # Each site owns a contiguous range of ItemIDs mapped to a category
    site_ranges = {
        1: (1, 333),    # Electronics
        2: (334, 666),  # Clothing
        3: (667, 1000)  # Food
    }

    for site_id, (start, end) in site_ranges.items():
        site_items = inventory[
            (inventory['ItemID'] >= start) & (inventory['ItemID'] <= end)
        ]
        db_path = os.path.join(DATA_DIR, f'site{site_id}.db')
        create_site_database(db_path, site_items)
        print(f"Site {site_id} loaded with {len(site_items)} items")

    # Step 6: Export full inventory to CSV for reference / analysis
    csv_path = os.path.join(DATA_DIR, 'inventory.csv')
    inventory.to_csv(csv_path, index=False)
    print(f"Full inventory exported to {csv_path}")

    # Print hot items info — these are the items used for conflict simulation
    hot_items = inventory[inventory['ItemID'] <= 10]
    print(f"\nHot items (ID 1-10) for conflict simulation:")
    print(hot_items[['ItemID', 'ItemName', 'Price']].to_string(index=False))


if __name__ == '__main__':
    main()
