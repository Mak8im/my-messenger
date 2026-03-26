from passlib.context import CryptContext
from sqlalchemy.orm import Session
from models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_user(db: Session, email: str, password: str):
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user:
        return None

    new_user = User(
        email=email,
        password=hash_password(password)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


def authenticate_user(db: Session, identifier: str, password: str):
    """
    Проверяет пользователя по email или username (с @)
    Пример: "user@mail.com" или "@username"
    """
    user = None
    
    # Если введено что-то с @ и похоже на email (содержит точку после @)
    if '@' in identifier and '.' in identifier:
        user = db.query(User).filter(User.email == identifier).first()
    else:
        # Иначе считаем, что это username (добавляем @ если отсутствует)
        username = identifier if identifier.startswith('@') else '@' + identifier
        user = db.query(User).filter(User.username == username).first()
    
    if not user:
        return None

    if not verify_password(password, user.password):
        return None

    return user
