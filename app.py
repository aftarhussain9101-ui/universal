import os
import logging
import uuid
import pickle
from datetime import datetime
from urllib.parse import quote

import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, ConversationHandler

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ===================== CONFIG =====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
FAMPAY_VPA = os.getenv("FAMPAY_VPA", "yourvpa@fampay")
MERCHANT_NAME = os.getenv("MERCHANT_NAME", "Indo Seller")

# ===================== FIREBASE =====================
if not firebase_admin._apps:
    cred = credentials.Certificate("service-account.json")
    firebase_admin.initialize_app(cred)
db = firestore.client()

# ===================== GMAIL =====================
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def get_gmail_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)

# ===================== ADD PRODUCT =====================
NAME, PRICE, DESC, STOCK = range(4)

async def addindo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Unauthorized")
    await update.message.reply_text("Enter Product Name:")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("Enter Price (₹):")
    return PRICE

async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['price'] = float(update.message.text)
    await update.message.reply_text("Enter Description:")
    return DESC

async def add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['desc'] = update.message.text
    await update.message.reply_text("Enter Stock Quantity:")
    return STOCK

async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stock = int(update.message.text)
    product_id = str(uuid.uuid4())[:8].upper()
    db.collection('products').document(product_id).set({
        "name": context.user_data['name'],
        "price": context.user_data['price'],
        "description": context.user_data['desc'],
        "stock": stock
    })
    await update.message.reply_text(f"✅ Product Added!\nID: `{product_id}`")
    return ConversationHandler.END

def check_gmail_for_utr(utr: str):
    try:
        service = get_gmail_service()
        results = service.users().messages().list(userId='me', q=utr, maxResults=10).execute()
        return len(results.get('messages', [])) > 0
    except:
        return False

# ===================== BOT =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to **Indo Seller Bot**!\nPay with FamPay UPI.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Shop Indos", callback_data="shop")]])
    )

async def shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = list(db.collection('products').where('stock', '>', 0).stream())
    if not products:
        return await query.edit_message_text("❌ No products available.")
    buttons = [[InlineKeyboardButton(f"{p.to_dict()['name']} - ₹{p.to_dict()['price']}", callback_data=f"buy_{p.id}")] for p in products]
    await query.edit_message_text("🛍 **Available Indos**", reply_markup=InlineKeyboardMarkup(buttons))

# ... (handle_buy, paid_handler, receive_utr functions are same as previous version)

async def handle_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = query.data.split("_")[1]
    product = db.collection('products').document(product_id).get().to_dict()
    ref_id = f"INDO{datetime.now().strftime('%H%M%S')}{uuid.uuid4().hex[:6].upper()}"
    
    db.collection('purchases').document(ref_id).set({
        "user_id": query.from_user.id,
        "product_name": product['name'],
        "amount": product['price'],
        "status": "pending"
    })

    upi_link = f"upi://pay?pa={FAMPAY_VPA}&pn={quote(MERCHANT_NAME)}&am={product['price']}&tr={ref_id}&tn=Buy {quote(product['name'])}&cu=INR"
    
    await query.edit_message_text(
        f"🛒 Order Created\nProduct: **{product['name']}**\nAmount: **₹{product['price']}**\nRef: `{ref_id}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now", url=upi_link)],
            [InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{ref_id}")]
        ])
    )

async def paid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ref_id = query.data.split("_")[1]
    context.user_data['pending_ref'] = ref_id
    await query.edit_message_text("Send your **UTR Number**:")

async def receive_utr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utr = update.message.text.strip()
    ref_id = context.user_data.get('pending_ref')
    if not ref_id:
        return await update.message.reply_text("Session expired.")

    await update.message.reply_text("🔍 Checking Gmail...")

    if check_gmail_for_utr(utr):
        await update.message.reply_text("🎉 Payment Verified!\n\n🔑 **Your Indo:**\nUsername: demo_user123\nPassword: SecurePass2026!")
        db.collection('purchases').document(ref_id).update({"status": "success", "utr": utr})
    else:
        await update.message.reply_text("❌ UTR not found in email yet.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addindo", addindo_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_desc)],
            STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock)],
        },
        fallbacks=[],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(shop, pattern="shop"))
    app.add_handler(CallbackQueryHandler(handle_buy, pattern="buy_"))
    app.add_handler(CallbackQueryHandler(paid_handler, pattern="paid_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_utr))

    print("✅ Universal Bot Started!")
    app.run_polling()

if __name__ == "__main__":
    main()