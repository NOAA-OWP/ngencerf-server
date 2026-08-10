import logging

from django.contrib.auth import get_user_model
from djoser.serializers import UserSerializer, UserCreateSerializer
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

logger = logging.getLogger(__name__)
User = get_user_model()


class CustomUserCreateSerializer(UserCreateSerializer):
    class Meta(UserCreateSerializer.Meta):
        model = User
        fields = ("id", "email", "first_name", "last_name", "password")
        extra_kwargs = {'password': {'write_only': True}}

    def validate(self, attrs):
        logger.info(
            "User registration validate: email=%r provided_keys=%s",
            attrs.get("email"),
            sorted(attrs.keys()),
        )

        try:
            return super().validate(attrs)
        except serializers.ValidationError as e:
            logger.warning(
                "User registration validation failed: email=%r detail=%r",
                attrs.get("email"),
                getattr(e, "detail", None),
            )
            raise

    def create(self, validated_data):
        logger.info(
            "User registration create: email=%r provided_keys=%s",
            validated_data.get("email"),
            sorted(validated_data.keys()),
        )

        # Automatically set username to email
        validated_data['username'] = validated_data['email']

        # Call the base implementation of create to ensure password hashing and other logic is applied
        return super().create(validated_data)


class CustomUserSerializer(UserSerializer):
    class Meta(UserSerializer.Meta):
        model = User
        fields = ("first_name", "last_name")


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):

    def validate(self, attrs):
        # Snapshot incoming keys/values safely
        login_field = getattr(User, "USERNAME_FIELD", "username")
        raw_identifier = attrs.get(login_field)
        provided_keys = sorted(attrs.keys())

        # Never log password; only log whether it was supplied
        logger.info(
            "JWT login attempt: login_field=%s provided_keys=%s identifier=%r password_supplied=%s",
            login_field,
            provided_keys,
            raw_identifier,
            bool(attrs.get("password")),
        )

        if not raw_identifier:
            logger.warning(
                "JWT login attempt missing identifier for login_field=%s; provided_keys=%s",
                login_field,
                provided_keys,
            )

        # Optional: pre-check user existence by identifier (safe)
        if raw_identifier:
            try:
                # case-insensitive check is usually what people expect for emails/usernames
                exists = User.objects.filter(**{f"{login_field}__iexact": raw_identifier}).exists()
                logger.debug(
                    "JWT login precheck: user_exists=%s for %s=%r",
                    exists,
                    login_field,
                    raw_identifier,
                )
            except Exception:
                logger.exception("JWT login precheck failed for %s=%r", login_field, raw_identifier)

        try:
            data = super().validate(attrs)

        except AuthenticationFailed as e:
            logger.warning(
                "JWT login failed: login_field=%s identifier=%r detail=%r",
                login_field,
                raw_identifier,
                getattr(e, "detail", None),
            )
            raise

        except serializers.ValidationError as e:
            # SimpleJWT uses ValidationError for bad credentials; Djoser may wrap it.
            logger.warning(
                "JWT login failed: login_field=%s identifier=%r detail=%r",
                login_field,
                raw_identifier,
                getattr(e, "detail", None),
            )
            raise

        except Exception:
            logger.exception(
                "JWT login failed with unexpected error: login_field=%s identifier=%r",
                login_field,
                raw_identifier,
            )
            raise

        # Success
        logger.info(
            "JWT login success: %s, %s=%r, is_active=%s is_staff=%s",
            getattr(self.user, "id", None),
            login_field,
            getattr(self.user, login_field, None),
            getattr(self.user, "is_active", None),
            getattr(self.user, "is_staff", None),
        )

        data["first_name"] = self.user.first_name
        data["last_name"] = self.user.last_name
        return data
